from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

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
            PAYMENT_API_KEY:
              description: Key for the payments provider
              secret: true
          services:
            postgres:
              image: postgres:16-alpine
              env: {POSTGRES_USER: app, POSTGRES_PASSWORD: app, POSTGRES_DB: app}
              port: 5432
              healthcheck: pg_isready -U app
        """
    ),
    "requirements.txt": "fastapi\nsqlalchemy\npsycopg[binary]\n",
    ".env.example": "DATABASE_URL=\n",
    "app/__init__.py": "",
    "app/settings.py": textwrap.dedent(
        """\
        from pydantic_settings import BaseSettings


        class Settings(BaseSettings):
            database_url: str
        """
    ),
    "app/main.py": textwrap.dedent(
        """\
        import os
        from fastapi import FastAPI

        app = FastAPI(title=os.getenv("APP_NAME", "shop"))


        @app.get("/health")
        def health():
            return {"ok": True}
        """
    ),
    "alembic/versions/001_init.py": "revision = '001'\n",
}


@pytest.fixture
def backend_files() -> dict[str, str]:
    return dict(BACKEND_FILES)


@pytest.fixture
def backend_repo(tmp_path: Path) -> tuple[Path, Path]:
    return init_remote(tmp_path, "backend", BACKEND_FILES)
