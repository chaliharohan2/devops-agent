"""Developer-to-developer sharing on the local side (phase 2).

Sharing (the teammate who was asked):
  * ``branch``       -> ``git bundle create`` of that branch (read-only on the repo)
  * ``working_tree`` -> tar.gz of tracked + untracked-but-not-ignored files, i.e.
                        exactly what is on disk now, including uncommitted edits.
Nothing is shared until the developer explicitly approves the request.

Receiving (the requester): download, verify sha256, unpack into the agent's own
``~/.devops-agent/peers/<id>/`` directory, then use it as a component source for
a test environment.
"""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
from pathlib import Path

from devops_common.gitguard import ReadOnlyGit, WorkspaceGit
from devops_common.protocol import PeerRequestView, PeerShareKind

from .config import LocalConfig
from .server_client import ServerClient

MAX_SNAPSHOT_BYTES = 250 * 1024 * 1024


class PeerShareError(RuntimeError):
    pass


def snapshot_working_tree(repo: Path) -> tuple[bytes, str]:
    g = ReadOnlyGit(repo)
    files = [f for f in g.run("ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.split("\0") if f]
    head = g.try_rev_parse("HEAD") or "unknown"
    dirty = bool(g.status_porcelain())
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in files:
            p = repo / rel
            if not p.is_file() or p.is_symlink():
                continue
            total += p.stat().st_size
            if total > MAX_SNAPSHOT_BYTES:
                raise PeerShareError("working tree snapshot exceeds 250 MB; share a branch instead")
            tar.add(str(p), arcname=rel, recursive=False)
    return buf.getvalue(), head + ("+dirty" if dirty else "")


def bundle_branch(repo: Path, ref: str) -> tuple[bytes, str]:
    g = ReadOnlyGit(repo)
    sha = g.try_rev_parse(f"refs/heads/{ref}") or g.try_rev_parse(f"refs/remotes/origin/{ref}")
    if sha is None:
        raise PeerShareError(f"branch {ref!r} not found in {repo}")
    ref_name = f"refs/heads/{ref}" if g.try_rev_parse(f"refs/heads/{ref}") else f"refs/remotes/origin/{ref}"
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "share.bundle"
        g.run("bundle", "create", str(out), ref_name, timeout=600)
        return out.read_bytes(), sha


def share(cfg: LocalConfig, server: ServerClient, pr: PeerRequestView) -> PeerRequestView:
    """Approve-and-upload, called only after the developer said yes."""
    repo = cfg.repo_for(pr.app, pr.component)
    if repo is None:
        raise PeerShareError(f"{pr.app}/{pr.component} is not linked to a local checkout")
    if pr.share == PeerShareKind.BRANCH:
        data, head = bundle_branch(repo, pr.ref or "")
    else:
        data, head = snapshot_working_tree(repo)
    if pr.status.value == "pending":
        server.peer_respond(pr.id, True)
    return server.peer_upload(pr.id, data, head)


def fetch(cfg: LocalConfig, server: ServerClient, pr_id: int, component_path: str = ".") -> Path:
    """Download a shared artifact and unpack it; returns the component source dir."""
    dest_root = cfg.peers_dir / str(pr_id)
    shutil.rmtree(dest_root, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)
    artifact, meta = server.peer_download(pr_id, dest_root / "artifact")
    src = dest_root / "src"
    if meta.get("share") == PeerShareKind.BRANCH.value:
        clone = dest_root / "repo"
        wg = WorkspaceGit(clone, cfg.peers_dir)
        wg.clone(str(artifact))
        head = meta.get("head_sha") or "HEAD"
        wg.export_tree(head, src, ".")
        shutil.rmtree(clone, ignore_errors=True)
    else:
        src.mkdir(parents=True, exist_ok=True)
        with tarfile.open(artifact, mode="r:gz") as tar:
            tar.extractall(src, filter="data")
    base = component_path.strip("/").strip(".")
    return (src / base) if base else src
