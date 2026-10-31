"""Change requests (deploy requests from local agents) and deployments."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from devops_common.protocol import ChangeRequestStatus, ChangeRequestView, DeployRequest, TimelineEntry

from ..db import get_db
from ..models import ChangeRequest, Deployment
from ..schemas import ApproveIn, DeployIn, DeploymentOut, RejectIn
from ..security import encrypt
from ..services import audit, config_store, deployer, orchestrator
from ..services.jobs import jobs
from .deps import Principal, get_app_or_404, get_env_or_404, get_principal, require

router = APIRouter()


def cr_view(cr: ChangeRequest) -> ChangeRequestView:
    return ChangeRequestView(
        id=cr.id, app=cr.app.name, component=cr.component.name, environment=cr.environment.name,
        source_branch=cr.source_branch, target_branch=cr.target_branch, head_sha=cr.head_sha,
        status=ChangeRequestStatus(cr.status), requested_by=cr.requested_by.username,
        risk_level=orchestrator.risk_or_none(cr), review=orchestrator.review_or_none(cr),
        discrepancies=cr.discrepancies or [], config_keys=[v["key"] for v in cr.proposed_config or []],
        merge_sha=cr.merge_sha, deployment_id=cr.deployment_id, error=cr.error,
        timeline=[TimelineEntry.model_validate(t) for t in cr.timeline or []],
        created_at=cr.created_at, updated_at=cr.updated_at,
    )


@router.post("/change-requests", response_model=ChangeRequestView, status_code=202, tags=["change-requests"])
def create_change_request(body: DeployRequest, db: Session = Depends(get_db),
                          p: Principal = Depends(get_principal)) -> ChangeRequestView:
    """Called by a local agent after the developer pushed. Starts the server agent pipeline."""
    app = get_app_or_404(db, body.app)
    env = get_env_or_404(app, body.environment)
    comp = next((c for c in app.components if c.name == body.component), None)
    if comp is None:
        raise HTTPException(404, f"component {body.component!r} not found")
    if body.report.head_sha != body.head_sha:
        raise HTTPException(422, "report head_sha does not match request head_sha")
    try:
        config_store.validate(body.config_values)
    except config_store.ConfigValidationError as e:
        raise HTTPException(422, str(e)) from e
    proposed = []
    for v in body.config_values:
        d = v.model_dump()
        if v.is_secret and v.value is not None:
            d["value"] = encrypt(v.value)
        proposed.append(d)
    cr = ChangeRequest(
        app_id=app.id, component_id=comp.id, environment_id=env.id, source_branch=body.source_branch,
        target_branch=env.branch_for(comp.name), head_sha=body.head_sha, status=ChangeRequestStatus.RECEIVED.value,
        requested_by_id=p.user.id, agent_id=p.agent_id, local_report=body.report.model_dump(mode="json"),
        proposed_config=proposed, notes=body.notes[:4000],
        timeline=[{"at": datetime.now(UTC).isoformat(), "status": "received", "message": f"from {p.name}"}],
    )
    db.add(cr)
    db.flush()
    audit.record(db, p.name, "change_request.create", "change_request", cr.id, app=app.name, component=comp.name,
                 environment=env.name, source=body.source_branch, head=body.head_sha,
                 config_keys=[v.key for v in body.config_values])
    db.commit()
    jobs.submit(orchestrator.process_change_request, cr.id)
    db.refresh(cr)
    return cr_view(cr)


@router.get("/change-requests", response_model=list[ChangeRequestView], tags=["change-requests"])
def list_change_requests(app: str | None = None, status: str | None = None, mine: bool = False,
                         limit: int = Query(50, le=500), db: Session = Depends(get_db),
                         p: Principal = Depends(get_principal)) -> list[ChangeRequestView]:
    q = select(ChangeRequest).order_by(ChangeRequest.id.desc()).limit(limit)
    if app:
        q = q.where(ChangeRequest.app_id == get_app_or_404(db, app).id)
    if status:
        q = q.where(ChangeRequest.status == status)
    if mine:
        q = q.where(ChangeRequest.requested_by_id == p.user.id)
    return [cr_view(cr) for cr in db.scalars(q)]


def _get_cr(db: Session, cr_id: int) -> ChangeRequest:
    cr = db.get(ChangeRequest, cr_id)
    if cr is None:
        raise HTTPException(404, "change request not found")
    return cr


@router.get("/change-requests/{cr_id}", response_model=ChangeRequestView, tags=["change-requests"])
def get_change_request(cr_id: int, db: Session = Depends(get_db), _: Principal = Depends(get_principal)) -> ChangeRequestView:
    return cr_view(_get_cr(db, cr_id))


@router.get("/change-requests/{cr_id}/reports", tags=["change-requests"])
def get_reports(cr_id: int, db: Session = Depends(get_db), _: Principal = Depends(get_principal)) -> dict:
    cr = _get_cr(db, cr_id)
    return {"local": cr.local_report, "server": cr.server_report, "missing_config": cr.missing_config,
            "approval_reasons": cr.approval_reasons}


@router.post("/change-requests/{cr_id}/approve", response_model=ChangeRequestView, tags=["change-requests"])
def approve(cr_id: int, body: ApproveIn, db: Session = Depends(get_db),
            p: Principal = Depends(require("maintainer"))) -> ChangeRequestView:
    cr = _get_cr(db, cr_id)
    if cr.requested_by_id == p.user.id and p.user.role != "admin":
        raise HTTPException(403, "you cannot approve your own change request")
    try:
        orchestrator.approve(db, cr, p.user.username, override_missing=body.override_missing)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    jobs.submit(orchestrator.execute_change_request, cr.id)
    db.refresh(cr)
    return cr_view(cr)


@router.post("/change-requests/{cr_id}/reject", response_model=ChangeRequestView, tags=["change-requests"])
def reject(cr_id: int, body: RejectIn, db: Session = Depends(get_db),
           p: Principal = Depends(require("maintainer"))) -> ChangeRequestView:
    cr = _get_cr(db, cr_id)
    try:
        orchestrator.reject(db, cr, p.user.username, body.reason)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    return cr_view(cr)


@router.post("/change-requests/{cr_id}/retry", response_model=ChangeRequestView, tags=["change-requests"])
def retry(cr_id: int, db: Session = Depends(get_db), p: Principal = Depends(get_principal)) -> ChangeRequestView:
    cr = _get_cr(db, cr_id)
    if cr.requested_by_id != p.user.id and p.user.role == "developer":
        raise HTTPException(403, "only the requester or a maintainer can retry")
    if cr.status not in (ChangeRequestStatus.FAILED.value, ChangeRequestStatus.CONFLICT.value):
        raise HTTPException(409, f"only failed/conflicted requests can be retried (is {cr.status})")
    orchestrator._set(cr, ChangeRequestStatus.RECEIVED, f"retry requested by {p.user.username}")
    cr.error = None
    audit.record(db, p.name, "change_request.retry", "change_request", cr.id)
    db.commit()
    jobs.submit(orchestrator.process_change_request, cr.id)
    return cr_view(cr)


# ----------------------------------------------------------------------------- deployments


def dep_out(d: Deployment) -> DeploymentOut:
    return DeploymentOut(id=d.id, app=d.app.name, environment=d.environment.name, change_request_id=d.change_request_id,
                         status=d.status, target=d.target, component_shas=d.component_shas or {},
                         config_version=d.config_version, plan=d.plan or {}, log=d.log or "", diagnosis=d.diagnosis,
                         triggered_by=d.triggered_by, created_at=d.created_at, started_at=d.started_at,
                         finished_at=d.finished_at)


@router.get("/deployments", response_model=list[DeploymentOut], tags=["deployments"])
def list_deployments(app: str | None = None, environment: str | None = None, limit: int = Query(50, le=500),
                     db: Session = Depends(get_db), _: Principal = Depends(get_principal)) -> list[DeploymentOut]:
    q = select(Deployment).order_by(Deployment.id.desc()).limit(limit)
    if app:
        a = get_app_or_404(db, app)
        q = q.where(Deployment.app_id == a.id)
        if environment:
            q = q.where(Deployment.environment_id == get_env_or_404(a, environment).id)
    return [dep_out(d) for d in db.scalars(q)]


@router.get("/deployments/{dep_id}", response_model=DeploymentOut, tags=["deployments"])
def get_deployment(dep_id: int, db: Session = Depends(get_db), _: Principal = Depends(get_principal)) -> DeploymentOut:
    d = db.get(Deployment, dep_id)
    if d is None:
        raise HTTPException(404, "deployment not found")
    return dep_out(d)


@router.post("/apps/{app_name}/environments/{env_name}/deploy", response_model=DeploymentOut, status_code=202,
             tags=["deployments"])
def deploy_now(app_name: str, env_name: str, body: DeployIn, db: Session = Depends(get_db),
               p: Principal = Depends(require("maintainer"))) -> DeploymentOut:
    """Redeploy the environment's current branches (e.g. after a config-only change)."""
    app = get_app_or_404(db, app_name)
    env = get_env_or_404(app, env_name)
    d = Deployment(app_id=app.id, environment_id=env.id, triggered_by=p.user.username, component_shas=body.component_shas)
    db.add(d)
    db.flush()
    audit.record(db, p.name, "deployment.request", "deployment", d.id, app=app.name, environment=env.name)
    db.commit()
    jobs.submit(_run_deployment, d.id)
    db.refresh(d)
    return dep_out(d)


def _run_deployment(dep_id: int) -> None:
    from ..db import session_scope

    with session_scope() as db:
        d = db.get(Deployment, dep_id)
        if d is not None:
            deployer.run_deployment(db, d)
