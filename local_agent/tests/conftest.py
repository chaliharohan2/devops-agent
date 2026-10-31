"""Local agent integration fixtures: an in-process server agent + git remotes + developer agents."""

from __future__ import annotations

import os
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_common.testing import init_remote

BACKEND = {
    "devops-agent.yaml": textwrap.dedent(
        """\
        version: 1
        app: shop
        component:
          name: backend
          kind: backend
          language: python
          framework: fastapi
          app_module: app.main:app
          port: 8000
          dev: {command: "uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload", mount: /app}
          healthcheck: {path: /health}
          migrations: {command: alembic upgrade head}
          depends_on: [postgres]
          env:
            DATABASE_URL:
              required: true
              local: postgresql+psycopg://app:app@${services.postgres.host}:5432/app
            CORS_ORIGINS:
              local: ${components.frontend.external_url}
          services:
            postgres:
              image: postgres:16-alpine
              env: {POSTGRES_USER: app, POSTGRES_PASSWORD: app, POSTGRES_DB: app}
              port: 5432
              healthcheck: pg_isready -U app
        """
    ),
    "requirements.txt": "fastapi\nuvicorn\n",
    "app/__init__.py": "",
    "app/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n",
}

FRONTEND = {
    "devops-agent.yaml": textwrap.dedent(
        """\
        version: 1
        app: shop
        component:
          name: frontend
          kind: frontend
          language: typescript
          framework: react
          port: 5173
          dev: {command: "npm run dev -- --host 0.0.0.0 --port 5173", port: 5173}
          depends_on: [backend]
          env:
            VITE_API_URL: {local: "${components.backend.external_url}"}
        """
    ),
    "package.json": '{"name": "shop-frontend", "scripts": {"dev": "vite"}, "devDependencies": {"vite": "7"}}\n',
    "src/main.tsx": "console.log(import.meta.env.VITE_API_URL)\n",
}


@dataclass
class World:
    client: TestClient
    tmp: Path
    backend_remote: Path
    frontend_remote: Path
    admin: dict[str, str]

    def user_token(self, username: str, role: str = "developer") -> str:
        r = self.client.post("/api/v1/users", json={"username": username, "role": role}, headers=self.admin)
        assert r.status_code == 201, r.text
        return r.json()["token"]

    def agent_for(self, username: str, *, runner=None, role: str = "developer"):
        """A LocalAgent with its own home dir and its own clones, talking to the in-process server."""
        from devops_common.testing import git
        from devops_local.config import LocalConfig
        from devops_local.service import LocalAgent

        token = self.user_token(username, role)
        home = self.tmp / f"home-{username}"
        home.mkdir()
        os.environ["DEVOPS_AGENT_HOME"] = str(home)
        cfg = LocalConfig(server_url="http://testserver")
        cfg.llm.enabled = False
        bootstrap = _server_client(self.client, token)
        agent = LocalAgent(cfg, server=bootstrap, runner=runner)
        reg = bootstrap.register(None, {})
        cfg.agent_id, cfg.agent_token, cfg.username = reg.agent_id, reg.agent_token, reg.username
        agent.server = _server_client(self.client, reg.agent_token)
        agent.envs.server = agent.server
        for name, remote in (("backend", self.backend_remote), ("frontend", self.frontend_remote)):
            clone = home / "code" / f"shop-{name}"
            clone.parent.mkdir(parents=True, exist_ok=True)
            git(self.tmp, "clone", "-q", str(remote), str(clone))
        cfg.search_paths = [str(home / "code")]
        return agent


def _server_client(client: TestClient, token: str):
    from devops_local.server_client import ServerClient

    sc = ServerClient("http://testserver", token)
    sc._http = TestClient(client.app, base_url="http://testserver/api/v1",
                          headers={"Authorization": f"Bearer {token}"})
    return sc


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    db_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'server.sqlite3'}"
    for k, v in {
        "DATABASE_URL": db_url, "DEVOPS_DATA_DIR": str(tmp_path / "server-data"), "DEVOPS_SYNC_JOBS": "true",
        "DEVOPS_LLM_ENABLED": "false", "DEVOPS_DEFAULT_DEPLOY_TARGET": "dry_run",
        "DEVOPS_BOOTSTRAP_ADMIN_TOKEN": "dxa_test_admin", "DEVOPS_GIT_SYNC_TTL_S": "0",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DEVOPS_AGENT_HOME", str(tmp_path / "home-default"))
    from devops_server import db as dbmod
    from devops_server import (
        models,  # noqa: F401
        security,
    )
    from devops_server.services import llm_service
    from devops_server.settings import get_settings

    get_settings.cache_clear()
    llm_service.get_client.cache_clear()
    security._fernet.cache_clear()
    dbmod.configure(db_url)
    dbmod.Base.metadata.drop_all(dbmod.engine())
    dbmod.Base.metadata.create_all(dbmod.engine())
    from devops_server.main import bootstrap_admin, create_app

    bootstrap_admin()
    be_remote, _ = init_remote(tmp_path, "shop-backend", BACKEND)
    fe_remote, _ = init_remote(tmp_path, "shop-frontend", FRONTEND)
    admin = {"Authorization": "Bearer dxa_test_admin"}
    with TestClient(create_app(init_db=False)) as client:
        r = client.post("/api/v1/apps", headers=admin, json={
            "name": "shop",
            "components": [
                {"name": "backend", "kind": "backend", "repo_url": str(be_remote)},
                {"name": "frontend", "kind": "frontend", "repo_url": str(fe_remote)},
            ],
            "environments": [{"name": "dev", "branch": "main", "deploy_target": {"type": "dry_run"}},
                             {"name": "staging", "branch": "main", "requires_approval": True}],
        })
        assert r.status_code == 201, r.text
        yield World(client=client, tmp=tmp_path, backend_remote=be_remote, frontend_remote=fe_remote, admin=admin)
    dbmod.Base.metadata.drop_all(dbmod.engine())
    get_settings.cache_clear()
