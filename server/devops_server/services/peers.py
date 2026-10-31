"""Developer-to-developer code sharing (phase 2), brokered by the server.

Local agents sit behind NAT/VPNs, so peers never connect directly. Instead:

1. Alice's agent asks the server for Bob's ``frontend`` (a branch, or his working tree).
2. Bob's agent sees the request in its inbox; **Bob must approve** in his app.
3. Bob's agent uploads a git bundle (branch) or a tarball (working tree snapshot,
   tracked + untracked-but-not-ignored files) — produced with read-only git.
4. Alice's agent downloads it (sha256-verified) and builds her test environment
   with Bob's code. Artifacts expire after ``peer_request_ttl_hours``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from devops_common.protocol import PeerRequestCreate, PeerRequestView, PeerShareKind
from devops_common.protocol import PeerRequestStatus as P

from ..models import App, PeerRequest, User
from ..settings import get_settings
from . import audit


class PeerError(ValueError):
    pass


def view(pr: PeerRequest) -> PeerRequestView:
    return PeerRequestView(
        id=pr.id, app=pr.app, component=pr.component, from_user=pr.from_user.username, to_user=pr.to_user.username,
        share=PeerShareKind(pr.share), ref=pr.ref, message=pr.message, status=P(pr.status),
        artifact_sha256=pr.artifact_sha256, artifact_size=pr.artifact_size, head_sha=pr.head_sha,
        created_at=pr.created_at, expires_at=pr.expires_at,
    )


def _expired(pr: PeerRequest) -> bool:
    if pr.expires_at is None:
        return False
    exp = pr.expires_at if pr.expires_at.tzinfo else pr.expires_at.replace(tzinfo=UTC)
    return exp < datetime.now(UTC)


def create(db: Session, from_user: User, req: PeerRequestCreate) -> PeerRequest:
    to_user = db.scalar(select(User).where(User.username == req.to_user, User.active.is_(True)))
    if to_user is None:
        raise PeerError(f"unknown user {req.to_user!r}")
    if to_user.id == from_user.id:
        raise PeerError("you cannot request code from yourself")
    app = db.scalar(select(App).where(App.name == req.app))
    if app is None or req.component not in {c.name for c in app.components}:
        raise PeerError(f"unknown app/component {req.app}/{req.component}")
    if req.share == PeerShareKind.BRANCH and not req.ref:
        raise PeerError("branch shares need a ref (branch name)")
    pr = PeerRequest(
        app=req.app, component=req.component, from_user_id=from_user.id, to_user_id=to_user.id,
        share=req.share.value, ref=req.ref, message=req.message[:2000], status=P.PENDING.value,
        expires_at=datetime.now(UTC) + timedelta(hours=get_settings().peer_request_ttl_hours),
    )
    db.add(pr)
    db.flush()
    audit.record(db, from_user.username, "peer.request", "peer_request", pr.id, to=to_user.username,
                 app=req.app, component=req.component, share=req.share.value, ref=req.ref)
    audit.notify(db, to_user.id, "peer_request", id=pr.id, from_user=from_user.username, app=req.app,
                 component=req.component, share=req.share.value, ref=req.ref, message=req.message)
    return pr


def respond(db: Session, pr: PeerRequest, user: User, approve: bool) -> None:
    if pr.to_user_id != user.id:
        raise PeerError("only the developer who was asked can respond")
    if pr.status != P.PENDING.value or _expired(pr):
        raise PeerError(f"request is {pr.status}")
    pr.status = (P.APPROVED if approve else P.DENIED).value
    audit.record(db, user.username, f"peer.{pr.status}", "peer_request", pr.id)
    audit.notify(db, pr.from_user_id, "peer_response", id=pr.id, status=pr.status, by=user.username,
                 app=pr.app, component=pr.component)


def store_artifact(db: Session, pr: PeerRequest, user: User, data: bytes, head_sha: str | None) -> None:
    if pr.to_user_id != user.id:
        raise PeerError("only the sharing developer can upload")
    if pr.status not in (P.APPROVED.value, P.UPLOADED.value) or _expired(pr):
        raise PeerError(f"request is {pr.status}; approve it first")
    limit = get_settings().peer_artifact_max_mb * 1024 * 1024
    if len(data) > limit:
        raise PeerError(f"artifact is larger than {get_settings().peer_artifact_max_mb} MB")
    d = get_settings().artifacts_dir
    d.mkdir(parents=True, exist_ok=True)
    ext = "bundle" if pr.share == PeerShareKind.BRANCH.value else "tar.gz"
    path = d / f"peer-{pr.id}.{ext}"
    path.write_bytes(data)
    pr.artifact_path = str(path)
    pr.artifact_sha256 = hashlib.sha256(data).hexdigest()
    pr.artifact_size = len(data)
    pr.head_sha = head_sha
    pr.status = P.UPLOADED.value
    audit.record(db, user.username, "peer.uploaded", "peer_request", pr.id, size=len(data), sha256=pr.artifact_sha256)
    audit.notify(db, pr.from_user_id, "peer_ready", id=pr.id, by=user.username, app=pr.app, component=pr.component)


def artifact_for_download(db: Session, pr: PeerRequest, user: User) -> Path:
    if pr.from_user_id != user.id:
        raise PeerError("only the requesting developer can download")
    if pr.status not in (P.UPLOADED.value, P.FETCHED.value) or not pr.artifact_path or _expired(pr):
        raise PeerError(f"artifact not available (request is {pr.status})")
    pr.status = P.FETCHED.value
    audit.record(db, user.username, "peer.fetched", "peer_request", pr.id)
    return Path(pr.artifact_path)


def expire_old(db: Session) -> int:
    n = 0
    for pr in db.scalars(select(PeerRequest).where(PeerRequest.status.notin_([P.EXPIRED.value, P.DENIED.value]))):
        if _expired(pr):
            if pr.artifact_path:
                Path(pr.artifact_path).unlink(missing_ok=True)
            pr.status = P.EXPIRED.value
            n += 1
    return n
