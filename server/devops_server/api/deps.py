from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Agent, ApiToken, App, Environment, User
from ..security import hash_token, role_at_least


@dataclass
class Principal:
    user: User
    agent_id: int | None = None

    @property
    def name(self) -> str:
        return f"{self.user.username}" + (f" (agent #{self.agent_id})" if self.agent_id else "")


def get_principal(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    row = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token), ApiToken.revoked_at.is_(None)))
    if row is None or not row.user.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked token")
    now = datetime.now(UTC)
    row.last_used_at = now
    if row.agent_id:
        agent = db.get(Agent, row.agent_id)
        if agent is not None:
            agent.last_seen_at = now
    db.commit()
    return Principal(user=row.user, agent_id=row.agent_id)


def require(role: str) -> Callable[[Principal], Principal]:
    def dep(p: Principal = Depends(get_principal)) -> Principal:
        if not role_at_least(p.user.role, role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role {role}")
        return p

    return dep


def get_app_or_404(db: Session, name: str) -> App:
    app = db.scalar(select(App).where(App.name == name))
    if app is None:
        raise HTTPException(404, f"app {name!r} not found")
    return app


def get_env_or_404(app: App, name: str) -> Environment:
    for e in app.environments:
        if e.name == name:
            return e
    raise HTTPException(404, f"environment {name!r} not found in app {app.name!r}")
