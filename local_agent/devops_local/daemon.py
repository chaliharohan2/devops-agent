"""Local agent daemon: a localhost-only HTTP API + the developer app UI.

Binds to 127.0.0.1 only. State-changing requests must carry the header
``X-DevOps-Agent: 1``; browsers cannot add custom headers cross-origin without a
CORS preflight (which this server never grants to foreign origins), so other
websites cannot drive the agent (CSRF protection).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from devops_common.protocol import ConfigValue

from . import __version__, hardware, model_manager
from .brain import Brain
from .envbuilder import EnvError
from .server_client import ServerError
from .service import AgentError, LocalAgent

log = logging.getLogger("devops_local")

ALLOWED_ORIGINS = {"http://127.0.0.1:7600", "http://localhost:7600", "http://localhost:5173", "http://127.0.0.1:5173"}


class AnalyzeIn(BaseModel):
    app: str | None = None
    component: str | None = None
    environment: str
    branch: str | None = None


class DeployIn(AnalyzeIn):
    config_values: list[ConfigValue] = Field(default_factory=list)
    notes: str = ""


class EnvIn(BaseModel):
    app: str
    environment: str = "dev"
    local_components: list[str] = Field(default_factory=list)
    peer_requests: dict[str, int] = Field(default_factory=dict)
    name: str | None = None


class LinkIn(BaseModel):
    app: str
    component: str
    path: str


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None


class PeerIn(BaseModel):
    app: str
    component: str
    from_user: str
    branch: str | None = None
    message: str = ""


class ModelPullIn(BaseModel):
    key: str | None = None


def create_app(agent: LocalAgent | None = None, *, background: bool = True) -> FastAPI:
    agent = agent or LocalAgent()
    brain = Brain(agent)
    jobs: dict[str, dict[str, Any]] = {}
    app = FastAPI(title="DevOps Agent — Local Agent", version=__version__)
    app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS), allow_methods=["*"],
                       allow_headers=["*"])

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):  # noqa: ANN001
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if request.headers.get("x-devops-agent") != "1" or (origin and origin not in ALLOWED_ORIGINS):
                return JSONResponse({"detail": "missing X-DevOps-Agent header or foreign origin"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(AgentError)
    @app.exception_handler(EnvError)
    async def agent_error(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ServerError)
    async def server_error(_: Request, exc: ServerError) -> JSONResponse:
        return JSONResponse({"detail": f"server agent: {exc.detail}"}, status_code=502 if exc.status in (0, 500) else exc.status)

    def run_job(kind: str, fn, *args: Any) -> str:  # noqa: ANN001
        job_id = f"{kind}-{int(time.time() * 1000)}"
        jobs[job_id] = {"id": job_id, "kind": kind, "status": "running", "result": None, "error": None, "log": []}

        def target() -> None:
            try:
                res = fn(*args, log=lambda m: jobs[job_id]["log"].append(m)) if kind == "env" else fn(*args)
                jobs[job_id]["result"] = res.to_dict() if hasattr(res, "to_dict") else res
                jobs[job_id]["status"] = "done"
            except Exception as e:  # noqa: BLE001 - reported to the UI
                log.exception("job %s failed", job_id)
                jobs[job_id].update(status="failed", error=str(e))

        threading.Thread(target=target, daemon=True).start()
        return job_id

    # ---------------------------------------------------------------- status
    @app.get("/api/ping")
    def ping() -> dict[str, Any]:
        """Cheap liveness probe (the UI uses it to detect developer mode)."""
        return {"ok": True, "service": "devops-local-agent", "version": __version__}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        out = agent.status()
        out["version"] = __version__
        out["docker"] = agent.docker_available()
        return out

    @app.get("/api/hardware")
    def hw() -> dict[str, Any]:
        h = hardware.detect()
        rec, notes = hardware.recommend(h)
        return {"hardware": h.to_dict(), "recommended": asdict(rec), "notes": notes,
                "catalog": [asdict(m) for m in hardware.CATALOG]}

    @app.get("/api/model")
    def model() -> dict[str, Any]:
        return model_manager.status(agent.cfg)

    @app.post("/api/model/pull")
    def model_pull(body: ModelPullIn) -> dict[str, Any]:
        opt = hardware.by_key(body.key) if body.key else hardware.recommend(hardware.detect())[0]

        def do() -> dict[str, Any]:
            model_manager.pull(agent.cfg, opt)
            agent.save()
            return {"model": agent.cfg.llm.model, "source": opt.source}

        return {"job": run_job("model", do)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        if job_id not in jobs:
            raise HTTPException(404, "job not found")
        return jobs[job_id]

    # ---------------------------------------------------------------- server
    @app.get("/api/apps")
    def apps() -> list[dict[str, Any]]:
        return agent.server.apps()

    @app.get("/api/repos")
    def list_repos() -> dict[str, str]:
        return agent.cfg.repos

    @app.post("/api/repos/link")
    def link(body: LinkIn) -> dict[str, str]:
        agent.link(body.app, body.component, body.path)
        return agent.cfg.repos

    @app.post("/api/repos/auto-link")
    def auto_link() -> dict[str, str]:
        return agent.auto_link()

    # ---------------------------------------------------------------- deploy
    @app.post("/api/changes/analyze")
    def analyze(body: AnalyzeIn) -> dict[str, Any]:
        a, c = agent.resolve(body.app, body.component)
        return agent.analyze(a, c, body.environment, body.branch)

    @app.post("/api/deploys")
    def deploy(body: DeployIn) -> dict[str, Any]:
        a, c = agent.resolve(body.app, body.component)
        return agent.deploy(a, c, body.environment, body.config_values, body.branch, body.notes).model_dump(mode="json")

    @app.get("/api/deploys")
    def deploys() -> list[dict[str, Any]]:
        return [cr.model_dump(mode="json") for cr in agent.deploy_status()]

    @app.get("/api/deploys/{cr_id}")
    def deploy_get(cr_id: int) -> dict[str, Any]:
        return agent.deploy_status(cr_id)[0].model_dump(mode="json")

    # ------------------------------------------------------------- test envs
    @app.get("/api/envs")
    def envs() -> list[dict[str, Any]]:
        return [e.to_dict() for e in agent.envs.list()]

    @app.post("/api/envs")
    def env_create(body: EnvIn) -> dict[str, Any]:
        return {"job": run_job("env", agent.env_up, body.app, body.environment, body.local_components,
                               body.peer_requests or None, body.name)}

    @app.get("/api/envs/{name}")
    def env_get(name: str) -> dict[str, Any]:
        return agent.envs.get(name).to_dict()

    @app.get("/api/envs/{name}/logs")
    def env_logs(name: str, service: str | None = None, tail: int = 200) -> dict[str, str]:
        return {"logs": agent.envs.logs(name, service, tail)}

    @app.delete("/api/envs/{name}")
    def env_delete(name: str) -> dict[str, Any]:
        return agent.env_down(name).to_dict()

    # ----------------------------------------------------------------- peers
    @app.get("/api/peers")
    def peers() -> list[dict[str, Any]]:
        return [p.model_dump(mode="json") for p in agent.server.peers()]

    @app.post("/api/peers")
    def peer_request(body: PeerIn) -> dict[str, Any]:
        return agent.peer_request(body.app, body.component, body.from_user, branch=body.branch,
                                  message=body.message).model_dump(mode="json")

    @app.post("/api/peers/{pr_id}/respond")
    def peer_respond(pr_id: int, approve: bool) -> dict[str, Any]:
        return agent.peer_respond(pr_id, approve).model_dump(mode="json")

    # ------------------------------------------------------------ chat/inbox
    @app.post("/api/chat")
    def chat(body: ChatIn) -> dict[str, Any]:
        return brain.chat(body.message, body.session_id)

    @app.get("/api/notifications")
    def notifications() -> list[dict[str, Any]]:
        return list(reversed(agent.notifications))

    if background:
        def poll() -> None:
            last_beat = 0.0
            while True:
                try:
                    if agent.cfg.agent_token:
                        agent.poll_inbox()
                        if time.monotonic() - last_beat > 60:
                            agent.server.heartbeat(agent.cfg.llm.source or agent.cfg.llm.model, {"version": __version__})
                            last_beat = time.monotonic()
                except Exception as e:  # noqa: BLE001 - keep polling
                    log.debug("inbox poll failed: %s", e)
                time.sleep(5)

        threading.Thread(target=poll, daemon=True, name="inbox-poller").start()

    ui = Path(__file__).resolve().parents[2] / "web" / "dist"
    if ui.is_dir():
        app.mount("/", StaticFiles(directory=ui, html=True), name="ui")
    return app
