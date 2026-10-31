"""Helpers for tests and demos: build throwaway git remotes and developer clones.

These functions act as the *developer* (they commit and push). Agents never use
them — agents only get the guarded wrappers in :mod:`devops_common.gitguard`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Dev Eloper",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Dev Eloper",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(cwd: Path | str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_ENV)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def init_remote(base: Path, name: str, files: dict[str, str], branch: str = "main") -> tuple[Path, Path]:
    """Create ``<base>/remotes/<name>.git`` seeded with ``files``; return (remote, dev_clone)."""
    remote = base / "remotes" / f"{name}.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    git(base, "init", "--bare", "-q", "-b", branch, str(remote))
    seed = base / "seed" / name
    seed.mkdir(parents=True, exist_ok=True)
    git(seed, "init", "-q", "-b", branch)
    write_files(seed, files)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "initial commit")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", branch)
    clone = base / "dev" / name
    clone.parent.mkdir(parents=True, exist_ok=True)
    git(base, "clone", "-q", str(remote), str(clone))
    return remote, clone


def commit_and_push(repo: Path, files: dict[str, str], message: str, branch: str,
                    delete: list[str] | None = None, push: bool = True) -> str:
    current = git(repo, "branch", "--show-current")
    if current != branch:
        exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=repo,
                                capture_output=True).returncode == 0
        git(repo, "checkout", "-q", *([] if exists else ["-b"]), branch)
    write_files(repo, files)
    for rel in delete or []:
        (repo / rel).unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    if push:
        git(repo, "push", "-q", "-u", "origin", branch)
    return git(repo, "rev-parse", "HEAD")
