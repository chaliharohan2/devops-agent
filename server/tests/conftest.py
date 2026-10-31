"""Server agent test fixtures.

By default tests use a throwaway SQLite file. Set ``TEST_DATABASE_URL`` to run
against PostgreSQL (CI does this), e.g.::

    TEST_DATABASE_URL=postgresql+psycopg://devops:devops@127.0.0.1:55432/devops_agent_test uv run pytest server/tests
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from devops_common.testing import init_remote

BACKEND_FILES = {
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
          migrations: {command: alembic upgrade head}
          depends_on: [postgres]
          env:
            DATABASE_URL:
              required: true
              local: postgresql+psycopg://app:app@${services.postgres.host}:5432/app
          services:
            postgres:
              image: postgres:16-alpine
              env: {POSTGRES_USER: app, POSTGRES_PASSWORD: app, POSTGRES_DB: app}
              port: 5432
              healthcheck: pg_isready -U app
        """
    ),
    "requirements.txt": "fastapi\nsqlalchemy\n",
    "app/__init__.py": "",
    "app/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n",
    "alembic/versions/001_init.py": "revision = '001'\n",
}


@dataclass
class Env:
    client: TestClient
    tmp: Path
    remote: Path
    dev_clone: Path
    admin: dict[str, str]

    def headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def user(self, username: str, role: str = "developer") -> dict[str, str]:
        r = self.client.post("/api/v1/users", json={"username": username, "role": role}, headers=self.admin)
        assert r.status_code == 201, r.text
        return self.headers(r.json()["token"])


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    db_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'server.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DEVOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEVOPS_SYNC_JOBS", "true")
    monkeypatch.setenv("DEVOPS_LLM_ENABLED", "false")
    monkeypatch.setenv("DEVOPS_DEFAULT_DEPLOY_TARGET", "dry_run")
    monkeypatch.setenv("DEVOPS_BOOTSTRAP_ADMIN_TOKEN", "dxa_test_admin_token")
    monkeypatch.setenv("DEVOPS_GIT_SYNC_TTL_S", "0")

    from devops_server import db as dbmod
    from devops_server import security
    from devops_server.services import llm_service
    from devops_server.settings import get_settings

    get_settings.cache_clear()
    llm_service.get_client.cache_clear()
    security._fernet.cache_clear()
    dbmod.configure(db_url)
    from devops_server import models  # noqa: F401

    dbmod.Base.metadata.drop_all(dbmod.engine())
    dbmod.Base.metadata.create_all(dbmod.engine())

    from devops_server.main import bootstrap_admin, create_app

    bootstrap_admin()
    remote, dev = init_remote(tmp_path, "backend", BACKEND_FILES)
    app = create_app(init_db=False)
    with TestClient(app) as client:
        yield Env(client=client, tmp=tmp_path, remote=remote, dev_clone=dev,
                  admin={"Authorization": "Bearer dxa_test_admin_token"})
    dbmod.Base.metadata.drop_all(dbmod.engine())
    get_settings.cache_clear()


@pytest.fixture
def shop(server: Env) -> Env:
    """App 'shop' with a backend component and dev (auto) + prod (approval) environments."""
    r = server.client.post(
        "/api/v1/apps",
        json={
            "name": "shop",
            "components": [{"name": "backend", "kind": "backend", "repo_url": str(server.remote)}],
            "environments": [
                {"name": "dev", "branch": "main", "deploy_target": {"type": "dry_run"}},
                {"name": "prod", "branch": "main", "requires_approval": True, "deploy_target": {"type": "dry_run"}},
            ],
        },
        headers=server.admin,
    )
    assert r.status_code == 201, r.text
    return server
