"""Local agent configuration, stored at ``~/.devops-agent/config.yaml``.

``DEVOPS_AGENT_HOME`` overrides the directory (used by tests and when running
several agents on one machine for demos).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


def agent_home() -> Path:
    return Path(os.environ.get("DEVOPS_AGENT_HOME", Path.home() / ".devops-agent")).expanduser()


class LLMConfig(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434/v1"  # Ollama's OpenAI-compatible endpoint
    model: str = "devops-agent-slm"  # alias created by `devops-agent model pull`
    source: str | None = None  # the pulled model (e.g. hf.co/ornith-ai/Ornith-1.5-9B-GGUF:Q4_K_M)
    api_key: str | None = None
    context_tokens: int = 16384
    timeout_s: float = 180.0


class LocalConfig(BaseModel):
    server_url: str = "http://127.0.0.1:8080"
    username: str | None = None
    agent_id: int | None = None
    agent_token: str | None = None
    llm: LLMConfig = Field(default_factory=LLMConfig)
    repos: dict[str, str] = Field(default_factory=dict)  # "app/component" -> local checkout path
    search_paths: list[str] = Field(default_factory=lambda: [str(Path.home() / "code"), str(Path.home() / "src")])
    ui_port: int = 7600
    docker_bin: str = "docker"
    build_network: str | None = None  # "host" behind corporate proxies
    port_range_start: int = 18000
    inbox_cursor: int = 0

    @property
    def home(self) -> Path:
        return agent_home()

    @property
    def workspace_dir(self) -> Path:
        return self.home / "workspaces"

    @property
    def envs_dir(self) -> Path:
        return self.home / "envs"

    @property
    def peers_dir(self) -> Path:
        return self.home / "peers"

    def repo_for(self, app: str, component: str) -> Path | None:
        p = self.repos.get(f"{app}/{component}")
        return Path(p).expanduser() if p else None


def config_path() -> Path:
    return agent_home() / "config.yaml"


def load_config() -> LocalConfig:
    p = config_path()
    if p.is_file():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg = LocalConfig.model_validate(data)
    else:
        cfg = LocalConfig()
    if os.environ.get("DEVOPS_AGENT_SERVER_URL"):
        cfg.server_url = os.environ["DEVOPS_AGENT_SERVER_URL"]
    return cfg


def save_config(cfg: LocalConfig) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    os.chmod(p, 0o600)  # contains the agent token
