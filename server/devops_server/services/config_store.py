"""Versioned, per-environment configuration store.

Configuration lives in Postgres, not in application repositories. That is what
lets the server agent "update configs" without ever touching code:

* each environment has a monotonically increasing ``config_version``;
* every change writes a :class:`ConfigRevision` (values masked) for history/rollback;
* secret values are encrypted at rest (Fernet) and never returned by the API;
* values are scoped app-wide (``*``) or to one component; component scope wins.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from devops_common.manifest import is_valid_env_key
from devops_common.protocol import ConfigValue

from ..models import ConfigEntry, ConfigRevision, Environment
from ..security import MASK, decrypt, encrypt
from . import audit

APP_SCOPE = "*"


class ConfigValidationError(ValueError):
    pass


@dataclass
class ResolvedConfig:
    env: dict[str, str]
    secret_keys: list[str]


def _scope(component: str | None) -> str:
    return component or APP_SCOPE


def list_entries(db: Session, env: Environment) -> list[ConfigEntry]:
    return list(
        db.scalars(
            select(ConfigEntry).where(ConfigEntry.environment_id == env.id).order_by(ConfigEntry.scope, ConfigEntry.key)
        )
    )


def has_key(db: Session, env: Environment, key: str, component: str | None) -> bool:
    q = select(ConfigEntry.id).where(
        ConfigEntry.environment_id == env.id,
        ConfigEntry.key == key,
        ConfigEntry.scope.in_([APP_SCOPE, _scope(component)]),
    )
    return db.scalar(q) is not None


def validate(values: list[ConfigValue]) -> None:
    for v in values:
        if not is_valid_env_key(v.key):
            raise ConfigValidationError(f"invalid config key {v.key!r}: use UPPER_SNAKE_CASE")
        if v.value is not None and len(v.value) > 64_000:
            raise ConfigValidationError(f"value for {v.key} is too large")


def apply(
    db: Session,
    env: Environment,
    values: list[ConfigValue],
    *,
    actor: str,
    message: str = "",
    change_request_id: int | None = None,
    deletes: list[tuple[str | None, str]] | None = None,
) -> int:
    """Upsert ``values`` (``value=None`` keeps the existing value) and bump the version."""
    validate(values)
    if not values and not deletes:
        return env.config_version
    new_version = (env.config_version or 0) + 1
    changes = []
    for v in values:
        scope = _scope(v.component)
        entry = db.scalar(
            select(ConfigEntry).where(
                ConfigEntry.environment_id == env.id, ConfigEntry.scope == scope, ConfigEntry.key == v.key
            )
        )
        if v.value is None and entry is None:
            continue  # nothing to set yet (caller only declared the key)
        action = "update" if entry else "create"
        if entry is None:
            entry = ConfigEntry(environment_id=env.id, scope=scope, key=v.key)
            db.add(entry)
        old_masked = (MASK if entry.is_secret else entry.value) if action == "update" else None
        if v.value is not None:
            entry.value = encrypt(v.value) if v.is_secret else v.value
            entry.is_secret = v.is_secret
        if v.description:
            entry.description = v.description
        entry.version = new_version
        entry.updated_by = actor
        changes.append({
            "action": action, "scope": scope, "key": v.key, "is_secret": entry.is_secret,
            "old": old_masked, "new": MASK if entry.is_secret else entry.value,
        })
    for component, key in deletes or []:
        entry = db.scalar(
            select(ConfigEntry).where(
                ConfigEntry.environment_id == env.id, ConfigEntry.scope == _scope(component), ConfigEntry.key == key
            )
        )
        if entry is not None:
            changes.append({"action": "delete", "scope": entry.scope, "key": key, "is_secret": entry.is_secret,
                            "old": MASK if entry.is_secret else entry.value, "new": None})
            db.delete(entry)
    if not changes:
        return env.config_version
    env.config_version = new_version
    db.add(ConfigRevision(environment_id=env.id, version=new_version, changes=changes, actor=actor,
                          message=message, change_request_id=change_request_id))
    audit.record(db, actor, "config.update", "environment", env.id, version=new_version,
                 keys=[c["key"] for c in changes], change_request_id=change_request_id)
    db.flush()
    return new_version


def resolve(db: Session, env: Environment, component: str, *, include_secrets: bool) -> ResolvedConfig:
    """Effective config for one component: app-wide values overridden by component values."""
    out: dict[str, str] = {}
    secret_keys: list[str] = []
    rows = [e for e in list_entries(db, env) if e.scope in (APP_SCOPE, component)]
    rows.sort(key=lambda e: 0 if e.scope == APP_SCOPE else 1)
    for e in rows:
        if e.is_secret:
            if e.key not in secret_keys:
                secret_keys.append(e.key)
            if include_secrets:
                out[e.key] = decrypt(e.value)
            else:
                out.pop(e.key, None)
        else:
            out[e.key] = e.value
            if e.key in secret_keys:
                secret_keys.remove(e.key)
    return ResolvedConfig(env=out, secret_keys=sorted(secret_keys))
