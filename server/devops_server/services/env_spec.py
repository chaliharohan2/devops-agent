"""Build an :class:`EnvironmentSpec` — the recipe local agents use to recreate an environment.

For test environments the spec contains **no secret values**: secret keys are
listed so the local agent can stub them, but values never leave the server.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from devops_common.compose import services_from_manifests
from devops_common.gitguard import GitCommandError
from devops_common.manifest import MANIFEST_FILENAMES, ComponentManifest, parse_manifest
from devops_common.protocol import ComponentRuntimeSpec, EnvironmentSpec

from ..models import App, Component, Environment
from . import config_store, git_service

log = logging.getLogger(__name__)


def component_manifest_at(component: Component, sha: str | None) -> ComponentManifest:
    """Manifest from the repo at ``sha`` if present, else the cached copy, else a stub."""
    if component.repo_url and sha:
        try:
            g = git_service.read_git(component.repo_url)
            base = component.path.strip("/").strip(".")
            for name in MANIFEST_FILENAMES:
                text = g.show_file(sha, f"{base}/{name}" if base else name)
                if text:
                    m = parse_manifest(text)
                    for c in m.components:
                        if c.name == component.name or len(m.components) == 1:
                            return c.model_copy(update={"name": component.name})
        except (GitCommandError, ValueError, OSError) as e:
            log.warning("could not read manifest for %s@%s: %s", component.name, sha, e)
    if component.manifest:
        data = dict(component.manifest)
        data["name"] = component.name
        return ComponentManifest.model_validate(data)
    return ComponentManifest(name=component.name, kind=component.kind, path=component.path)


def build_spec(db: Session, app: App, env: Environment, *, include_secrets: bool,
               shas: dict[str, str] | None = None) -> EnvironmentSpec:
    comps: list[ComponentRuntimeSpec] = []
    manifests: list[ComponentManifest] = []
    notes: list[str] = []
    for c in app.components:
        ref = env.branch_for(c.name)
        sha = (shas or {}).get(c.name)
        if sha is None and c.repo_url:
            try:
                sha = git_service.branch_head(c.repo_url, ref)
            except (GitCommandError, OSError) as e:
                notes.append(f"{c.name}: cannot read {ref} from {c.repo_url}: {e}")
            if sha is None:
                notes.append(f"{c.name}: branch {ref!r} not found")
        m = component_manifest_at(c, sha)
        if m.model_dump(mode="json") != (c.manifest or {}):
            c.manifest = m.model_dump(mode="json")  # keep the cache fresh
        manifests.append(m)
        cfg = config_store.resolve(db, env, c.name, include_secrets=include_secrets)
        comps.append(
            ComponentRuntimeSpec(
                name=c.name, kind=m.kind, repo_url=c.repo_url, ref=ref, sha=sha,
                manifest=m.model_dump(mode="json"), env=cfg.env,
                secret_keys=[] if include_secrets else cfg.secret_keys,
            )
        )
    if not include_secrets:
        notes.append("Secret values are never sent to test environments; the local agent stubs them.")
    return EnvironmentSpec(
        app=app.name, environment=env.name, components=comps, services=services_from_manifests(manifests),
        config_version=env.config_version, generated_at=datetime.now(UTC), notes=notes,
    )
