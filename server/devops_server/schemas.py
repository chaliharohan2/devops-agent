"""REST request/response models specific to the server API (wire protocol lives in devops_common)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from devops_common.protocol import ComponentKind, ConfigValue


class UserCreate(BaseModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    display_name: str = ""
    role: str = Field(default="developer", pattern="^(developer|maintainer|admin)$")


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    active: bool


class TokenOut(BaseModel):
    user: UserOut
    token: str
    note: str = "Store this token now; it cannot be shown again."


class ComponentIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    kind: ComponentKind = ComponentKind.OTHER
    repo_url: str | None = None
    path: str = "."
    manifest: dict[str, Any] | None = None


class EnvironmentIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    branch: str = "main"
    branch_overrides: dict[str, str] = Field(default_factory=dict)
    requires_approval: bool = False
    auto_deploy: bool = True
    deploy_target: dict[str, Any] = Field(default_factory=dict)


class AppIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    description: str = ""
    components: list[ComponentIn] = Field(default_factory=list)
    environments: list[EnvironmentIn] = Field(default_factory=list)


class ImportRepoIn(BaseModel):
    repo_url: str
    branch: str = "main"


class ComponentOut(BaseModel):
    id: int
    name: str
    kind: str
    repo_url: str | None
    path: str
    manifest: dict[str, Any]


class EnvironmentOut(BaseModel):
    id: int
    name: str
    branch: str
    branch_overrides: dict[str, Any]
    requires_approval: bool
    auto_deploy: bool
    deploy_target: dict[str, Any]
    config_version: int


class AppOut(BaseModel):
    id: int
    name: str
    description: str
    components: list[ComponentOut]
    environments: list[EnvironmentOut]


class ConfigEntryOut(BaseModel):
    scope: str
    key: str
    value: str | None
    is_secret: bool
    description: str
    version: int
    updated_by: str
    updated_at: datetime | None


class ConfigDelete(BaseModel):
    key: str
    component: str | None = None


class ConfigUpdate(BaseModel):
    values: list[ConfigValue] = Field(default_factory=list)
    deletes: list[ConfigDelete] = Field(default_factory=list)
    message: str = ""


class ConfigRevisionOut(BaseModel):
    version: int
    actor: str
    message: str
    change_request_id: int | None
    changes: list[Any]
    created_at: datetime


class ApproveIn(BaseModel):
    override_missing: bool = False


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class DeployIn(BaseModel):
    component_shas: dict[str, str] = Field(default_factory=dict)


class DeploymentOut(BaseModel):
    id: int
    app: str
    environment: str
    change_request_id: int | None
    status: str
    target: str
    component_shas: dict[str, Any]
    config_version: int
    plan: dict[str, Any]
    log: str
    diagnosis: str | None
    triggered_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AuditOut(BaseModel):
    id: int
    ts: datetime
    actor: str
    action: str
    subject_type: str
    subject_id: str
    details: dict[str, Any]


class AgentOut(BaseModel):
    id: int
    username: str
    hostname: str
    version: str
    model: str | None
    capabilities: dict[str, Any]
    last_seen_at: datetime
    online: bool


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=8000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


class ChatOut(BaseModel):
    reply: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
