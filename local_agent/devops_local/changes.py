"""Deploy flow on the developer side.

The local agent **never commits or pushes**. It only inspects what the developer
already pushed, works out the operational impact, collects config values and
asks the server agent to merge + deploy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from devops_common.analysis import analyze_branch
from devops_common.gitguard import GitCommandError, ReadOnlyGit
from devops_common.manifest import ComponentManifest, load_manifest
from devops_common.protocol import ChangeReport, ConfigChangeKind, ConfigValue, DeployRequest


@dataclass
class Preflight:
    repo: str
    branch: str | None
    local_head: str | None = None
    pushed_head: str | None = None
    unpushed_commits: int = 0
    uncommitted: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # block the deploy
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def preflight(repo: Path, branch: str | None = None, target_branch: str | None = None) -> Preflight:
    g = ReadOnlyGit(repo)
    if not g.is_repo():
        return Preflight(repo=str(repo), branch=branch, problems=[f"{repo} is not a git repository"])
    branch = branch or g.current_branch()
    pf = Preflight(repo=str(repo), branch=branch)
    if not branch:
        pf.problems.append("Detached HEAD: check out the branch you want to deploy.")
        return pf
    try:
        g.fetch("origin")
    except GitCommandError as e:
        pf.warnings.append(f"git fetch failed ({e.stderr.strip()[:200]}); using last known remote state.")
    pf.local_head = g.try_rev_parse(f"refs/heads/{branch}")
    pf.pushed_head = g.try_rev_parse(f"refs/remotes/origin/{branch}")
    if target_branch and branch == target_branch:
        pf.problems.append(f"You are on {target_branch}, the environment's own branch. Deploy a feature branch.")
    if pf.pushed_head is None:
        pf.problems.append(f"Branch {branch!r} is not on the remote. Push it first: git push -u origin {branch}")
        return pf
    if pf.local_head and pf.local_head != pf.pushed_head:
        ahead = int(g.out("rev-list", "--count", f"{pf.pushed_head}..{pf.local_head}") or 0)
        behind = int(g.out("rev-list", "--count", f"{pf.local_head}..{pf.pushed_head}") or 0)
        pf.unpushed_commits = ahead
        if ahead:
            pf.warnings.append(f"{ahead} local commit(s) are not pushed and will NOT be deployed. "
                               f"Run `git push` first if you want them included.")
        if behind:
            pf.warnings.append(f"origin/{branch} has {behind} commit(s) you have not pulled; the pushed state is deployed.")
    pf.uncommitted = g.status_porcelain()
    if pf.uncommitted:
        pf.warnings.append(f"{len(pf.uncommitted)} uncommitted change(s) are not part of the deploy.")
    return pf


def component_manifest(repo: Path, app: str, component: str, server_manifest: dict | None) -> ComponentManifest:
    m = load_manifest(repo, app=app)
    if m is not None:
        for c in m.components:
            if c.name == component:
                return c
        if len(m.components) == 1:
            return m.components[0].model_copy(update={"name": component})
    if server_manifest:
        return ComponentManifest.model_validate({**server_manifest, "name": component})
    return ComponentManifest(name=component)


def analyze(repo: Path, *, app: str, component: str, branch: str, target_branch: str,
            server_manifest: dict | None = None, repo_url: str | None = None) -> ChangeReport:
    g = ReadOnlyGit(repo)
    manifest = component_manifest(repo, app, component, server_manifest)
    return analyze_branch(
        g, app=app, component=manifest, base_ref=f"refs/remotes/origin/{target_branch}",
        head_ref=f"refs/remotes/origin/{branch}", source_branch=branch, target_branch=target_branch,
        repo_url=repo_url or g.remote_url(),
    )


def values_needed(report: ChangeReport) -> list[dict]:
    """Config keys the developer should supply values for (with hints for the UI/CLI)."""
    out = []
    for c in report.config_changes:
        if c.kind == ConfigChangeKind.ENV_VAR_ADDED and c.key:
            out.append({"key": c.key, "secret": c.is_secret_hint, "default": c.default_value,
                        "required": c.requires_value, "description": c.description,
                        "evidence": [f"{e.file}:{e.line}" for e in c.evidence][:2]})
    return out


def build_request(report: ChangeReport, environment: str, values: list[ConfigValue], notes: str = "") -> DeployRequest:
    for v in values:
        v.component = v.component or report.component
    return DeployRequest(app=report.app, component=report.component, environment=environment,
                         source_branch=report.source_branch, head_sha=report.head_sha, report=report,
                         config_values=values, notes=notes)
