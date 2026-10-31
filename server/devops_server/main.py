"""FastAPI application factory for the server agent.

Run with::

    uvicorn devops_server.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import db as dbmod
from .api import api_router
from .models import ApiToken, User
from .security import hash_token
from .settings import get_settings

log = logging.getLogger("devops_server")


def bootstrap_admin() -> None:
    """Create the first admin from DEVOPS_BOOTSTRAP_ADMIN_TOKEN (idempotent)."""
    token = get_settings().bootstrap_admin_token
    if not token:
        return
    with dbmod.session_scope() as s:
        if s.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token))):
            return
        admin = s.scalar(select(User).where(User.username == "admin"))
        if admin is None:
            admin = User(username="admin", display_name="Administrator", role="admin")
            s.add(admin)
            s.flush()
        s.add(ApiToken(user_id=admin.id, name="bootstrap", kind="user", token_hash=hash_token(token), prefix=token[:10]))
        log.warning("bootstrap admin token registered for user 'admin'")


def create_app(*, init_db: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if init_db:
            dbmod.engine()
            if settings.database_url.startswith("sqlite"):
                dbmod.create_all()  # Postgres schema is managed by Alembic
            bootstrap_admin()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(
        title="DevOps Agent — Server Agent",
        version="0.1.0",
        description="Merges pushed branches, owns environment configuration, runs deployments and "
        "serves environment specs to developer agents.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    ui = Path(__file__).resolve().parents[2] / "web" / "dist"
    if ui.is_dir():
        app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
