from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditEvent, InboxEventRow

SERVER_AGENT = "server-agent"


def record(db: Session, actor: str, action: str, subject_type: str = "", subject_id: Any = "", **details: Any) -> None:
    """Append an audit event. Every agent action and every human decision is recorded."""
    db.add(AuditEvent(actor=actor, action=action, subject_type=subject_type, subject_id=str(subject_id),
                      details=details))


def notify(db: Session, user_id: int, kind: str, **payload: Any) -> None:
    """Queue an event for a user's local agent(s) (delivered via the polling inbox)."""
    db.add(InboxEventRow(user_id=user_id, kind=kind, payload=payload))
