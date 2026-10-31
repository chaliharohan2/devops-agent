"""Tokens, roles and secret encryption."""

from __future__ import annotations

import hashlib
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .settings import get_settings

ROLE_ORDER = {"developer": 0, "maintainer": 1, "admin": 2}
TOKEN_PREFIX = "dxa_"
MASK = "••••••"


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def role_at_least(role: str, required: str) -> bool:
    return ROLE_ORDER.get(role, -1) >= ROLE_ORDER[required]


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        # Development fallback: derive a stable key from the DB URL so restarts can
        # still decrypt. Production deployments MUST set DEVOPS_SECRET_KEY.
        import base64

        digest = hashlib.sha256(("dev-only:" + get_settings().database_url).encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as e:  # pragma: no cover - key rotation problems
        raise ValueError("cannot decrypt secret: wrong DEVOPS_SECRET_KEY?") from e


def mask(value: str | None, is_secret: bool) -> str | None:
    if value is None:
        return None
    return MASK if is_secret else value
