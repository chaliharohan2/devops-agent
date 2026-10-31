"""Operator chat with the server agent (vLLM + read-only tools)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from devops_common.agentloop import Tool, run_tool_loop
from devops_common.llm import LLMError

from ..db import get_db
from ..models import App, AuditEvent, ChangeRequest, Deployment
from ..schemas import ChatIn, ChatOut
from ..services import config_store, llm_service
from .deps import Principal, get_principal

router = APIRouter(tags=["agent"])

_OBJ = {"type": "object", "properties": {}}


def _tools(db: Session) -> list[Tool]:
    def list_apps(_: dict[str, Any]) -> Any:
        return [{"app": a.name, "components": [c.name for c in a.components],
                 "environments": [{"name": e.name, "branch": e.branch, "config_version": e.config_version}
                                  for e in a.environments]} for a in db.scalars(select(App))]

    def change_requests(args: dict[str, Any]) -> Any:
        q = select(ChangeRequest).order_by(ChangeRequest.id.desc()).limit(int(args.get("limit") or 10))
        if args.get("status"):
            q = q.where(ChangeRequest.status == args["status"])
        return [{"id": c.id, "app": c.app.name, "component": c.component.name, "env": c.environment.name,
                 "branch": c.source_branch, "status": c.status, "risk": c.risk_level, "by": c.requested_by.username,
                 "error": (c.error or "")[:300]} for c in db.scalars(q)]

    def change_request(args: dict[str, Any]) -> Any:
        c = db.get(ChangeRequest, int(args.get("id", 0)))
        if c is None:
            return {"error": "not found"}
        return {"id": c.id, "status": c.status, "review": c.review, "missing_config": c.missing_config,
                "discrepancies": c.discrepancies, "timeline": c.timeline[-8:], "error": c.error}

    def deployments(args: dict[str, Any]) -> Any:
        q = select(Deployment).order_by(Deployment.id.desc()).limit(int(args.get("limit") or 10))
        return [{"id": d.id, "app": d.app.name, "env": d.environment.name, "status": d.status, "target": d.target,
                 "shas": d.component_shas, "config_version": d.config_version, "diagnosis": d.diagnosis,
                 "log_tail": (d.log or "")[-800:]} for d in db.scalars(q)]

    def config_keys(args: dict[str, Any]) -> Any:
        app = db.scalar(select(App).where(App.name == args.get("app")))
        env = next((e for e in (app.environments if app else []) if e.name == args.get("environment")), None)
        if env is None:
            return {"error": "unknown app/environment"}
        return [{"scope": e.scope, "key": e.key, "secret": e.is_secret, "version": e.version}
                for e in config_store.list_entries(db, env)]

    def recent_audit(args: dict[str, Any]) -> Any:
        rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(int(args.get("limit") or 15)))
        return [{"ts": e.ts.isoformat(), "actor": e.actor, "action": e.action, "subject": f"{e.subject_type}:{e.subject_id}"}
                for e in rows]

    lim = {"limit": {"type": "integer", "description": "max rows"}}
    return [
        Tool("list_apps", "List apps, components and environments.", _OBJ, list_apps),
        Tool("list_change_requests", "Recent change (deploy) requests.",
             {"type": "object", "properties": {**lim, "status": {"type": "string"}}}, change_requests),
        Tool("get_change_request", "Details of one change request.",
             {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}, change_request),
        Tool("list_deployments", "Recent deployments with log tails.", {"type": "object", "properties": lim}, deployments),
        Tool("list_config_keys", "Config keys of an environment (values are never shown).",
             {"type": "object", "properties": {"app": {"type": "string"}, "environment": {"type": "string"}},
              "required": ["app", "environment"]}, config_keys),
        Tool("recent_audit_events", "Latest audit log entries.", {"type": "object", "properties": lim}, recent_audit),
    ]


@router.post("/agent/chat", response_model=ChatOut)
def chat(body: ChatIn, db: Session = Depends(get_db), p: Principal = Depends(get_principal)) -> ChatOut:
    client = llm_service.get_client()
    if client is None:
        raise HTTPException(503, "server LLM is not configured (set VLLM_BASE_URL, VLLM_MODEL, VLLM_API_KEY)")
    system = (llm_service.SYSTEM_PROMPT + f"\nYou are talking to {p.user.username} (role: {p.user.role}). "
              "Use the tools to answer from live data. Your tools are read-only; to approve or deploy, tell the user "
              "which dashboard action to take.")
    try:
        run = run_tool_loop(client, system, [m.model_dump() for m in body.messages], _tools(db), max_steps=5)
    except LLMError as e:
        raise HTTPException(502, f"LLM error: {e}") from e
    return ChatOut(reply=run.final, steps=[{"tool": s.tool, "arguments": s.arguments, "error": s.error} for s in run.steps])
