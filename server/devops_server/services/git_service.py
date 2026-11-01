"""Server-side repository cache.

The server agent keeps one clone per repository under ``<data_dir>/repos``. Every
operation goes through the guarded wrappers in :mod:`devops_common.gitguard`:
:class:`ReadOnlyGit` for analysis/exports and :class:`MergeGit` for merges.
A per-repository lock serialises merges so two change requests never race.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from devops_common.gitguard import MergeGit, ReadOnlyGit

from ..settings import get_settings

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_last_sync: dict[str, float] = {}


def repo_dir(url: str) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", url.rsplit("/", 1)[-1].removesuffix(".git")).strip("-")[:40] or "repo"
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    return get_settings().repos_dir / f"{slug}-{digest}"


def lock_for(url: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(url, threading.RLock())


def synced_merge_git(url: str, *, force: bool = False) -> MergeGit:
    """Clone if needed and fetch (rate limited by ``git_sync_ttl_s`` unless ``force``)."""
    s = get_settings()
    mg = MergeGit(repo_dir(url), identity=(s.git_merge_name, s.git_merge_email))
    with lock_for(url):
        mg.ensure_clone(url)
        now = time.monotonic()
        if force or now - _last_sync.get(url, 0) > s.git_sync_ttl_s:
            mg.sync()
            _last_sync[url] = now
    return mg


def read_git(url: str, *, force_sync: bool = False) -> ReadOnlyGit:
    synced_merge_git(url, force=force_sync)
    return ReadOnlyGit(repo_dir(url))


def branch_head(url: str, branch: str) -> str | None:
    return read_git(url).try_rev_parse(f"refs/remotes/origin/{branch}")
