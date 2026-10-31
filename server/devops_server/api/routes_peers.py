"""Developer-to-developer sharing (phase 2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from devops_common.protocol import PeerRequestCreate, PeerRequestView

from ..db import get_db
from ..models import PeerRequest
from ..services import peers
from .deps import Principal, get_principal

router = APIRouter(prefix="/peers", tags=["peers"])


def _get(db: Session, pr_id: int, p: Principal) -> PeerRequest:
    pr = db.get(PeerRequest, pr_id)
    if pr is None or p.user.id not in (pr.from_user_id, pr.to_user_id):
        raise HTTPException(404, "peer request not found")
    return pr


@router.post("", response_model=PeerRequestView, status_code=201)
def create(body: PeerRequestCreate, db: Session = Depends(get_db), p: Principal = Depends(get_principal)) -> PeerRequestView:
    try:
        pr = peers.create(db, p.user, body)
    except peers.PeerError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return peers.view(pr)


@router.get("", response_model=list[PeerRequestView])
def list_requests(db: Session = Depends(get_db), p: Principal = Depends(get_principal)) -> list[PeerRequestView]:
    peers.expire_old(db)
    db.commit()
    rows = db.scalars(select(PeerRequest).where(or_(PeerRequest.from_user_id == p.user.id,
                                                    PeerRequest.to_user_id == p.user.id))
                      .order_by(PeerRequest.id.desc()).limit(100))
    return [peers.view(r) for r in rows]


@router.post("/{pr_id}/respond", response_model=PeerRequestView)
def respond(pr_id: int, approve: bool, db: Session = Depends(get_db), p: Principal = Depends(get_principal)) -> PeerRequestView:
    pr = _get(db, pr_id, p)
    try:
        peers.respond(db, pr, p.user, approve)
    except peers.PeerError as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    return peers.view(pr)


@router.put("/{pr_id}/artifact", response_model=PeerRequestView)
async def upload(pr_id: int, request: Request, x_head_sha: str | None = Header(default=None),
                 db: Session = Depends(get_db), p: Principal = Depends(get_principal)) -> PeerRequestView:
    pr = _get(db, pr_id, p)
    data = await request.body()
    try:
        peers.store_artifact(db, pr, p.user, data, x_head_sha)
    except peers.PeerError as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    return peers.view(pr)


@router.get("/{pr_id}/artifact")
def download(pr_id: int, db: Session = Depends(get_db), p: Principal = Depends(get_principal)) -> FileResponse:
    pr = _get(db, pr_id, p)
    try:
        path = peers.artifact_for_download(db, pr, p.user)
    except peers.PeerError as e:
        raise HTTPException(409, str(e)) from e
    db.commit()
    return FileResponse(path, media_type="application/octet-stream", filename=path.name,
                        headers={"X-Artifact-Sha256": pr.artifact_sha256 or "", "X-Head-Sha": pr.head_sha or "",
                                 "X-Share-Kind": pr.share})
