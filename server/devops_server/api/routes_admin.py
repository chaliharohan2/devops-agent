"""Users, tokens, agent registration/inbox, audit log and health."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from devops_common.protocol import AgentRegistered, AgentRegistration, InboxEvent

from ..db import get_db
from ..models import Agent, ApiToken, AuditEvent, InboxEventRow, User
from ..schemas import AgentOut, AuditOut, TokenOut, UserCreate, UserOut
from ..security import hash_token, new_token
from ..services import audit, llm_service
from .deps import Principal, get_principal, require

router = APIRouter()


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, username=u.username, display_name=u.display_name, role=u.role, active=u.active)


def issue_token(db: Session, user: User, *, name: str, kind: str = "user", agent_id: int | None = None) -> str:
    token = new_token()
    db.add(ApiToken(user_id=user.id, agent_id=agent_id, name=name, kind=kind, token_hash=hash_token(token),
                    prefix=token[:10]))
    return token


@router.get("/health", tags=["system"])
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"ok": True, "service": "devops-server", "time": datetime.now(UTC).isoformat()}


@router.get("/llm/health", tags=["system"])
def llm_health(probe: bool = False, _: Principal = Depends(get_principal)) -> dict:
    return llm_service.health(probe=probe)


@router.get("/me", tags=["users"])
def me(p: Principal = Depends(get_principal)) -> dict:
    return {"user": user_out(p.user).model_dump(), "agent_id": p.agent_id}


@router.get("/users", tags=["users"], response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: Principal = Depends(get_principal)) -> list[UserOut]:
    return [user_out(u) for u in db.scalars(select(User).order_by(User.username))]


@router.post("/users", tags=["users"], response_model=TokenOut, status_code=201)
def create_user(body: UserCreate, db: Session = Depends(get_db), p: Principal = Depends(require("admin"))) -> TokenOut:
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, "user exists")
    u = User(username=body.username, display_name=body.display_name or body.username, role=body.role)
    db.add(u)
    db.flush()
    token = issue_token(db, u, name="initial")
    audit.record(db, p.name, "user.create", "user", u.id, username=u.username, role=u.role)
    db.commit()
    return TokenOut(user=user_out(u), token=token)


@router.post("/users/{username}/tokens", tags=["users"], response_model=TokenOut, status_code=201)
def create_token(username: str, db: Session = Depends(get_db), p: Principal = Depends(get_principal)) -> TokenOut:
    u = db.scalar(select(User).where(User.username == username))
    if u is None:
        raise HTTPException(404, "user not found")
    if u.id != p.user.id and p.user.role != "admin":
        raise HTTPException(403, "can only create tokens for yourself")
    token = issue_token(db, u, name="api")
    audit.record(db, p.name, "token.create", "user", u.id)
    db.commit()
    return TokenOut(user=user_out(u), token=token)


# ----------------------------------------------------------------------------- agents


@router.post("/agents/register", tags=["agents"], response_model=AgentRegistered)
def register_agent(body: AgentRegistration, db: Session = Depends(get_db),
                   p: Principal = Depends(get_principal)) -> AgentRegistered:
    agent = Agent(user_id=p.user.id, hostname=body.hostname[:255], version=body.version[:32], model=body.model,
                  capabilities=body.capabilities)
    db.add(agent)
    db.flush()
    token = issue_token(db, p.user, name=f"agent@{body.hostname}", kind="agent", agent_id=agent.id)
    audit.record(db, p.name, "agent.register", "agent", agent.id, hostname=body.hostname, model=body.model)
    db.commit()
    return AgentRegistered(agent_id=agent.id, agent_token=token, username=p.user.username)


@router.post("/agents/heartbeat", tags=["agents"])
def heartbeat(body: AgentRegistration | None = None, db: Session = Depends(get_db),
              p: Principal = Depends(get_principal)) -> dict:
    if p.agent_id and body is not None:
        agent = db.get(Agent, p.agent_id)
        if agent:
            agent.model, agent.version, agent.capabilities = body.model, body.version, body.capabilities
            db.commit()
    return {"ok": True}


@router.get("/agents", tags=["agents"], response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db), _: Principal = Depends(get_principal)) -> list[AgentOut]:
    cutoff = datetime.now(UTC) - timedelta(minutes=2)
    out = []
    for a in db.scalars(select(Agent).order_by(Agent.last_seen_at.desc())):
        seen = a.last_seen_at if a.last_seen_at.tzinfo else a.last_seen_at.replace(tzinfo=UTC)
        out.append(AgentOut(id=a.id, username=a.user.username, hostname=a.hostname, version=a.version, model=a.model,
                            capabilities=a.capabilities, last_seen_at=seen, online=seen > cutoff))
    return out


@router.get("/agents/inbox", tags=["agents"], response_model=list[InboxEvent])
def inbox(after: int = 0, limit: int = Query(50, le=200), db: Session = Depends(get_db),
          p: Principal = Depends(get_principal)) -> list[InboxEvent]:
    rows = db.scalars(
        select(InboxEventRow).where(InboxEventRow.user_id == p.user.id, InboxEventRow.id > after)
        .order_by(InboxEventRow.id).limit(limit)
    )
    return [InboxEvent(id=r.id, kind=r.kind, payload=r.payload, created_at=r.created_at) for r in rows]


# ----------------------------------------------------------------------------- audit


@router.get("/audit", tags=["audit"], response_model=list[AuditOut])
def list_audit(limit: int = Query(100, le=1000), action: str | None = None, db: Session = Depends(get_db),
               _: Principal = Depends(get_principal)) -> list[AuditOut]:
    q = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if action:
        q = q.where(AuditEvent.action.like(f"{action}%"))
    return [AuditOut(id=e.id, ts=e.ts, actor=e.actor, action=e.action, subject_type=e.subject_type,
                     subject_id=e.subject_id, details=e.details) for e in db.scalars(q)]
