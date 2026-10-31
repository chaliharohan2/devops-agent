"""SQLAlchemy models for the server agent (PostgreSQL)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, JSONType


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(16), default="developer")  # developer | maintainer | admin
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship()


class ApiToken(TimestampMixin, Base):
    __tablename__ = "api_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(16), default="user")  # user | agent
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship()


class App(TimestampMixin, Base):
    __tablename__ = "apps"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    components: Mapped[list[Component]] = relationship(back_populates="app", cascade="all, delete-orphan",
                                                       order_by="Component.id")
    environments: Mapped[list[Environment]] = relationship(back_populates="app", cascade="all, delete-orphan",
                                                           order_by="Environment.id")


class Component(TimestampMixin, Base):
    __tablename__ = "components"
    __table_args__ = (UniqueConstraint("app_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="other")
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    path: Mapped[str] = mapped_column(String(255), default=".")  # sub-dir in a monorepo
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # cached ComponentManifest
    app: Mapped[App] = relationship(back_populates="components")


class Environment(TimestampMixin, Base):
    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("app_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(255), default="main")
    branch_overrides: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # component -> branch
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_deploy: Mapped[bool] = mapped_column(Boolean, default=True)
    deploy_target: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # {"type": "compose", "host_ports": {...}}
    config_version: Mapped[int] = mapped_column(Integer, default=0)
    app: Mapped[App] = relationship(back_populates="environments")

    def branch_for(self, component: str) -> str:
        return (self.branch_overrides or {}).get(component) or self.branch


class ConfigEntry(Base):
    """Current value of one config key. ``scope`` is a component name or ``*`` (app-wide)."""

    __tablename__ = "config_entries"
    __table_args__ = (UniqueConstraint("environment_id", "scope", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(64), default="*")
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text, default="")  # Fernet ciphertext when is_secret
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ConfigRevision(TimestampMixin, Base):
    __tablename__ = "config_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    changes: Mapped[list[Any]] = mapped_column(JSONType, default=list)  # values masked
    actor: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, default="")
    change_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ChangeRequest(TimestampMixin, Base):
    __tablename__ = "change_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    component_id: Mapped[int] = mapped_column(ForeignKey("components.id", ondelete="CASCADE"))
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    source_branch: Mapped[str] = mapped_column(String(255))
    target_branch: Mapped[str] = mapped_column(String(255))
    head_sha: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True, default="received")
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    local_report: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    server_report: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    discrepancies: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    missing_config: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    review: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    proposed_config: Mapped[list[Any]] = mapped_column(JSONType, default=list)  # secret values encrypted
    notes: Mapped[str] = mapped_column(Text, default="")
    merge_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_reasons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeline: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    app: Mapped[App] = relationship()
    component: Mapped[Component] = relationship()
    environment: Mapped[Environment] = relationship()
    requested_by: Mapped[User] = relationship()


class Deployment(TimestampMixin, Base):
    __tablename__ = "deployments"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"), index=True)
    change_request_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|running|succeeded|failed
    target: Mapped[str] = mapped_column(String(32), default="compose")
    component_shas: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, default=0)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # rendered compose (secrets masked)
    log: Mapped[str] = mapped_column(Text, default="")
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[str] = mapped_column(String(64), default="server-agent")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    app: Mapped[App] = relationship()
    environment: Mapped[Environment] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(32), default="")
    subject_id: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class InboxEventRow(TimestampMixin, Base):
    __tablename__ = "inbox_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PeerRequest(TimestampMixin, Base):
    __tablename__ = "peer_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    app: Mapped[str] = mapped_column(String(64))
    component: Mapped[str] = mapped_column(String(64))
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    share: Mapped[str] = mapped_column(String(16), default="branch")
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    from_user: Mapped[User] = relationship(foreign_keys=[from_user_id])
    to_user: Mapped[User] = relationship(foreign_keys=[to_user_id])
