"""Map server-side components to the developer's local checkouts."""

from __future__ import annotations

import re
from pathlib import Path

from devops_common.gitguard import ReadOnlyGit

_SSH = re.compile(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$")
_HTTP = re.compile(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$")


def normalize_url(url: str | None) -> str | None:
    """`git@github.com:org/repo.git` == `https://github.com/org/repo` == `/abs/path/repo.git`."""
    if not url:
        return None
    u = url.strip()
    for pat in (_SSH, _HTTP):
        m = pat.match(u)
        if m:
            return f"{m.group(1).lower()}/{m.group(2).lower()}"
    if u.startswith("file://"):
        u = u[len("file://"):]
    p = Path(u).expanduser()
    return str(p.resolve()).removesuffix(".git").rstrip("/") if p.is_absolute() or u.startswith(".") else u.lower()


def discover(search_paths: list[str], max_depth: int = 3) -> dict[str, Path]:
    """Find git checkouts under ``search_paths``: normalized remote URL -> path."""
    found: dict[str, Path] = {}

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth or not d.is_dir():
            return
        if (d / ".git").exists():
            url = normalize_url(ReadOnlyGit(d).remote_url())
            if url:
                found.setdefault(url, d)
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith((".", "node_modules")))
        except OSError:
            return
        for c in children:
            walk(c, depth + 1)

    for sp in search_paths:
        walk(Path(sp).expanduser(), 0)
    return found


def auto_link(apps: list[dict], search_paths: list[str], existing: dict[str, str]) -> dict[str, str]:
    """Link components whose repo_url matches a discovered checkout. Returns new links."""
    checkouts = discover(search_paths)
    new: dict[str, str] = {}
    for app in apps:
        for comp in app.get("components", []):
            key = f"{app['name']}/{comp['name']}"
            if key in existing:
                continue
            path = checkouts.get(normalize_url(comp.get("repo_url")) or "")
            if path:
                new[key] = str(path)
    return new
