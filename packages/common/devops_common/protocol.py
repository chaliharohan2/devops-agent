"""Wire protocol shared by local agents and the server agent.

Everything that crosses the network between a developer's local agent and the
server agent is one of these models. Keeping them in one module (and one
package used by both sides) means the contract is versioned in a single place.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

PROTOCOL_VERSION = "1"


class ComponentKind(StrEnum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    WORKER = "worker"
    DATABASE = "database"
    CACHE = "cache"
    OTHER = "other"


# --------------------------------------------------------------------------- #
# Change analysis
# --------------------------------------------------------------------------- #


class ConfigChangeKind(StrEnum):
    ENV_VAR_ADDED = "env_var_added"
    ENV_VAR_REMOVED = "env_var_removed"
    CONFIG_FILE_CHANGED = "config_file_changed"
    DEPENDENCY_CHANGED = "dependency_changed"
    MIGRATION_ADDED = "migration_added"
    BUILD_CHANGED = "build_changed"
    MANIFEST_CHANGED = "manifest_changed"


class Evidence(BaseModel):
    """Where in the diff a finding came from (so humans and LLMs can verify it)."""

    file: str
    line: int | None = None
    snippet: str | None = None


class ConfigChange(BaseModel):
    kind: ConfigChangeKind
    component: str
    key: str | None = None  # env var name, or file path for file-level changes
    description: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    is_secret_hint: bool = False
    default_value: str | None = None  # e.g. value found in .env.example
    requires_value: bool = False  # True when an environment must supply a value


class FileChange(BaseModel):
    path: str
    status: str  # A, M, D, R
    additions: int = 0
    deletions: int = 0
    old_path: str | None = None


class CommitInfo(BaseModel):
    sha: str
    author: str
    email: str = ""
    date: str = ""
    subject: str


class ChangeReport(BaseModel):
    """Deterministic description of what a branch changes relative to its target."""

    app: str
    component: str
    repo_url: str | None = None
    source_branch: str
    target_branch: str
    base_sha: str
    head_sha: str
    commits: list[CommitInfo] = Field(default_factory=list)
    changed_files: list[FileChange] = Field(default_factory=list)
    config_changes: list[ConfigChange] = Field(default_factory=list)
    requires_rebuild: bool = False
    requires_migration: bool = False
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""
    analyzer_version: str = "1"

    def env_vars_added(self) -> list[str]:
        return sorted(
            {c.key for c in self.config_changes if c.kind == ConfigChangeKind.ENV_VAR_ADDED and c.key}
        )

    def fingerprint(self) -> dict[str, Any]:
        """The parts of a report that two independent analyses must agree on."""
        return {
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "files": sorted(f.path for f in self.changed_files),
            "config": sorted({f"{c.kind}:{c.key}" for c in self.config_changes}),
            "requires_rebuild": self.requires_rebuild,
            "requires_migration": self.requires_migration,
        }


# --------------------------------------------------------------------------- #
# Deploy / change requests
# --------------------------------------------------------------------------- #


class ConfigValue(BaseModel):
    key: str
    value: str | None = None
    is_secret: bool = False
    component: str | None = None  # None = app-wide
    description: str = ""


class DeployRequest(BaseModel):
    """Sent by a local agent: "please merge this pushed branch and update these configs"."""

    app: str
    component: str
    environment: str
    source_branch: str
    head_sha: str
    report: ChangeReport
    config_values: list[ConfigValue] = Field(default_factory=list)
    notes: str = ""
    protocol_version: str = PROTOCOL_VERSION


class ChangeRequestStatus(StrEnum):
    RECEIVED = "received"
    ANALYZING = "analyzing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    MERGING = "merging"
    MERGED = "merged"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            ChangeRequestStatus.DEPLOYED,
            ChangeRequestStatus.REJECTED,
            ChangeRequestStatus.CONFLICT,
            ChangeRequestStatus.FAILED,
        }


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewResult(BaseModel):
    """Server agent's review of a change request (LLM-produced, schema-validated)."""

    risk_level: RiskLevel = RiskLevel.MEDIUM
    summary: str = ""
    concerns: list[str] = Field(default_factory=list)
    missing_config: list[str] = Field(default_factory=list)
    deployment_steps: list[str] = Field(default_factory=list)
    source: str = "deterministic"  # "llm" or "deterministic"


class TimelineEntry(BaseModel):
    at: datetime
    status: str
    message: str = ""


class ChangeRequestView(BaseModel):
    id: int
    app: str
    component: str
    environment: str
    source_branch: str
    target_branch: str
    head_sha: str
    status: ChangeRequestStatus
    requested_by: str
    risk_level: RiskLevel | None = None
    review: ReviewResult | None = None
    discrepancies: list[str] = Field(default_factory=list)
    config_keys: list[str] = Field(default_factory=list)
    merge_sha: str | None = None
    deployment_id: int | None = None
    error: str | None = None
    timeline: list[TimelineEntry] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Test environment spec (server -> local agent)
# --------------------------------------------------------------------------- #


class ServiceSpec(BaseModel):
    """Infrastructure dependency (postgres, redis, ...) run from a stock image."""

    name: str
    image: str
    env: dict[str, str] = Field(default_factory=dict)
    port: int | None = None
    healthcheck: str | None = None
    volumes: list[str] = Field(default_factory=list)


class ComponentRuntimeSpec(BaseModel):
    name: str
    kind: ComponentKind = ComponentKind.OTHER
    repo_url: str | None = None
    ref: str  # branch the environment tracks
    sha: str | None = None  # exact commit last deployed/merged (if known)
    manifest: dict[str, Any] = Field(default_factory=dict)  # serialized ComponentManifest
    env: dict[str, str] = Field(default_factory=dict)  # non-secret config values
    secret_keys: list[str] = Field(default_factory=list)  # keys withheld from test envs


class EnvironmentSpec(BaseModel):
    app: str
    environment: str
    components: list[ComponentRuntimeSpec] = Field(default_factory=list)
    services: list[ServiceSpec] = Field(default_factory=list)
    config_version: int = 0
    generated_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Agents, inbox and peer-to-peer (phase 2)
# --------------------------------------------------------------------------- #


class AgentRegistration(BaseModel):
    hostname: str
    version: str
    model: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class AgentRegistered(BaseModel):
    agent_id: int
    agent_token: str
    username: str


class InboxEvent(BaseModel):
    id: int
    kind: str  # peer_request | peer_response | change_request_update
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PeerShareKind(StrEnum):
    BRANCH = "branch"  # committed refs, shipped as a git bundle
    WORKING_TREE = "working_tree"  # snapshot of tracked + untracked (non-ignored) files


class PeerRequestCreate(BaseModel):
    app: str
    component: str
    to_user: str
    share: PeerShareKind = PeerShareKind.BRANCH
    ref: str | None = None  # branch name when share == branch
    message: str = ""


class PeerRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    UPLOADED = "uploaded"
    FETCHED = "fetched"
    EXPIRED = "expired"


class PeerRequestView(BaseModel):
    id: int
    app: str
    component: str
    from_user: str
    to_user: str
    share: PeerShareKind
    ref: str | None = None
    message: str = ""
    status: PeerRequestStatus
    artifact_sha256: str | None = None
    artifact_size: int | None = None
    head_sha: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
