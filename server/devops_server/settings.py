"""Server agent configuration (environment variables, optionally a ``.env`` file)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings use the ``DEVOPS_`` prefix except the LLM endpoint (``VLLM_*``) and
    ``DATABASE_URL``, which are accepted under their conventional names."""

    model_config = SettingsConfigDict(env_prefix="DEVOPS_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- database -----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://devops:devops@localhost:5432/devops_agent",
        validation_alias=AliasChoices("DEVOPS_DATABASE_URL", "DATABASE_URL"),
        description="SQLAlchemy URL of the server agent's Postgres database",
    )

    # --- LLM (vLLM, OpenAI-compatible) ---------------------------------------
    # e.g. http://host:8000/v1 or http://host:8000/v1/chat/completions
    vllm_base_url: str | None = Field(default=None, validation_alias=AliasChoices("VLLM_BASE_URL", "DEVOPS_VLLM_BASE_URL"))
    vllm_api_key: str | None = Field(default=None, validation_alias=AliasChoices("VLLM_API_KEY", "DEVOPS_VLLM_API_KEY"))
    vllm_model: str | None = Field(default=None, validation_alias=AliasChoices("VLLM_MODEL", "DEVOPS_VLLM_MODEL"))
    llm_enabled: bool = True
    llm_timeout_s: float = 120.0
    # Extra request params that disable "thinking" for structured calls (Qwen3.x on vLLM).
    llm_no_think_params: dict[str, Any] = Field(default_factory=lambda: {"chat_template_kwargs": {"enable_thinking": False}})

    # --- security ------------------------------------------------------------
    # Fernet key for encrypting secret config values at rest. Generate with
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str | None = None
    bootstrap_admin_token: str | None = None  # creates an "admin" user on first start if set
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    # --- workspaces ------------------------------------------------------------
    data_dir: Path = Path("./.devops-agent/server")
    sync_jobs: bool = False  # run pipelines inline (tests / debugging)
    git_sync_ttl_s: float = 20.0
    # Identity for the merge commits the server agent creates. Set explicitly so merges
    # work on hosts without a git user.name / user.email (CI runners, fresh VMs).
    git_merge_name: str = "DevOps Server Agent"
    git_merge_email: str = "devops-agent@localhost"

    # --- deployments -----------------------------------------------------------
    default_deploy_target: str = "compose"  # compose | dry_run
    docker_bin: str = "docker"
    deploy_build_network: str | None = None  # e.g. "host" behind corporate proxies

    # --- peer artifacts ----------------------------------------------------------
    peer_artifact_max_mb: int = 300
    peer_request_ttl_hours: int = 24

    @field_validator("llm_no_think_params", mode="before")
    @classmethod
    def _parse_json(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else {}
        return v

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_enabled and self.vllm_base_url and self.vllm_model)

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def deploy_dir(self) -> Path:
        return self.data_dir / "deployments"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
