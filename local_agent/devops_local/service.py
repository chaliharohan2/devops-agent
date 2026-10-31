"""LocalAgent: the single facade used by the CLI, the HTTP daemon and the SLM's tools.

Capabilities (and, just as importantly, non-capabilities):

* reads the developer's repositories with :class:`ReadOnlyGit` — never commits or pushes;
* talks only to the server agent (deploy requests, env specs, peer brokering);
* runs Docker Compose for *local* test environments;
* never receives production secrets.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from devops_common.gitguard import ReadOnlyGit
from devops_common.protocol import (
    AgentRegistered,
    ChangeRequestView,
    ConfigValue,
    InboxEvent,
    PeerRequestCreate,
    PeerRequestView,
    PeerShareKind,
)

from . import changes, hardware, model_manager, peers, repos
from .config import LocalConfig, load_config, save_config
from .envbuilder import DockerRunner, EnvBuilder, EnvState
from .errors import AgentError
from .server_client import ServerClient, ServerError


class LocalAgent:
    def __init__(self, cfg: LocalConfig | None = None, server: ServerClient | None = None,
                 runner: DockerRunner | None = None):
        self.cfg = cfg or load_config()
        self.server = server or ServerClient(self.cfg.server_url, self.cfg.agent_token)
        self.envs = EnvBuilder(self.cfg, self.server, runner)
        self.notifications: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def save(self) -> None:
        save_config(self.cfg)

    # ------------------------------------------------------------------ setup
    def register(self, server_url: str, user_token: str) -> AgentRegistered:
        self.cfg.server_url = server_url
        hw = hardware.detect()
        reg = ServerClient(server_url, user_token).register(self.cfg.llm.source or self.cfg.llm.model,
                                                           {"hardware": hw.to_dict(), "docker": self.docker_available()})
        self.cfg.agent_id, self.cfg.agent_token, self.cfg.username = reg.agent_id, reg.agent_token, reg.username
        self.server = ServerClient(server_url, reg.agent_token)
        self.envs.server = self.server
        self.save()
        return reg

    def docker_available(self) -> bool:
        import shutil
        import subprocess

        if not shutil.which(self.cfg.docker_bin):
            return False
        try:
            return subprocess.run([self.cfg.docker_bin, "info"], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"server_url": self.cfg.server_url, "username": self.cfg.username,
                               "agent_id": self.cfg.agent_id, "repos": self.cfg.repos}
        try:
            self.server.health()
            out["server"] = "reachable"
        except ServerError as e:
            out["server"] = f"unreachable: {e.detail}"
        out["model"] = model_manager.status(self.cfg)
        out["environments"] = [{"name": e.name, "status": e.status, "urls": e.urls} for e in self.envs.list()]
        return out

    # ------------------------------------------------------------------ repos
    def link(self, app: str, component: str, path: str | Path) -> None:
        p = Path(path).expanduser().resolve()
        if not ReadOnlyGit(p).is_repo():
            raise AgentError(f"{p} is not a git checkout")
        self.cfg.repos[f"{app}/{component}"] = str(p)
        self.save()

    def auto_link(self) -> dict[str, str]:
        new = repos.auto_link(self.server.apps(), self.cfg.search_paths, self.cfg.repos)
        if new:
            self.cfg.repos.update(new)
            self.save()
        return new

    def _app(self, app: str) -> dict[str, Any]:
        try:
            return self.server.app(app)
        except ServerError as e:
            raise AgentError(e.detail) from e

    def resolve(self, app: str | None, component: str | None, cwd: Path | None = None) -> tuple[str, str]:
        """Fill in app/component from the current directory when the developer omits them."""
        if app and component:
            return app, component
        cwd = (cwd or Path(os.getcwd())).resolve()
        for key, path in self.cfg.repos.items():
            p = Path(path).resolve()
            if cwd == p or p in cwd.parents:
                a, c = key.split("/", 1)
                if (app is None or app == a) and (component is None or component == c):
                    return a, c
        links = [k.split("/", 1) for k in self.cfg.repos]
        if app and not component:
            mine = [c for a, c in links if a == app]
            if len(mine) == 1:
                return app, mine[0]
        if not app and len(links) == 1:
            return links[0][0], links[0][1]
        raise AgentError("Which app/component? Pass app/component or run inside a linked checkout "
                         "(devops-agent link <app>/<component> <path>).")

    # ----------------------------------------------------------------- deploy
    def target_branch(self, app: str, component: str, environment: str) -> str:
        data = self._app(app)
        env = next((e for e in data["environments"] if e["name"] == environment), None)
        if env is None:
            raise AgentError(f"{app} has no environment {environment!r} "
                             f"(available: {', '.join(e['name'] for e in data['environments'])})")
        return (env.get("branch_overrides") or {}).get(component) or env["branch"]

    def analyze(self, app: str, component: str, environment: str, branch: str | None = None) -> dict[str, Any]:
        repo = self.cfg.repo_for(app, component)
        if repo is None:
            raise AgentError(f"{app}/{component} is not linked to a local checkout")
        target = self.target_branch(app, component, environment)
        pf = changes.preflight(repo, branch, target)
        out: dict[str, Any] = {"app": app, "component": component, "environment": environment,
                               "target_branch": target, "preflight": asdict(pf)}
        if not pf.ok:
            return out
        comp = next((c for c in self._app(app)["components"] if c["name"] == component), {})
        report = changes.analyze(repo, app=app, component=component, branch=pf.branch or "", target_branch=target,
                                 server_manifest=comp.get("manifest"), repo_url=comp.get("repo_url"))
        if not report.commits:
            out["preflight"]["problems"].append(
                f"Nothing to deploy: {pf.branch} has no commits that are not already in {target}.")
        out["report"] = report.model_dump(mode="json")
        out["values_needed"] = changes.values_needed(report)
        return out

    def deploy(self, app: str, component: str, environment: str, values: list[ConfigValue] | None = None,
               branch: str | None = None, notes: str = "") -> ChangeRequestView:
        result = self.analyze(app, component, environment, branch)
        pf = result["preflight"]
        if pf["problems"]:
            raise AgentError(" ".join(pf["problems"]))
        from devops_common.protocol import ChangeReport

        report = ChangeReport.model_validate(result["report"])
        req = changes.build_request(report, environment, values or [], notes)
        try:
            return self.server.create_change_request(req)
        except ServerError as e:
            raise AgentError(e.detail) from e

    def deploy_status(self, cr_id: int | None = None) -> list[ChangeRequestView]:
        if cr_id is not None:
            return [self.server.change_request(cr_id)]
        return self.server.my_change_requests()

    # ---------------------------------------------------------- test envs
    def env_up(self, app: str, environment: str, local_components: list[str],
               peer_requests: dict[str, int] | None = None, name: str | None = None, *, start: bool = True,
               log: Callable[[str], None] | None = None) -> EnvState:
        peer_sources: dict[str, Path] = {}
        if peer_requests:
            comps = {c["name"]: c for c in self._app(app)["components"]}
            for comp, pr_id in peer_requests.items():
                path = (comps.get(comp, {}).get("manifest") or {}).get("path", ".")
                peer_sources[comp] = self.peer_fetch(pr_id, component_path=path)
        try:
            st, _ = self.envs.prepare(app, environment, local_components, peer_sources, name)
        except ServerError as e:
            raise AgentError(e.detail) from e
        if log:
            log(f"rendered {self.envs._dir(st.name) / 'docker-compose.yml'}")
        if start:
            st = self.envs.up(st, log=log)
        return st

    def env_down(self, name: str) -> EnvState:
        return self.envs.down(name)

    # ------------------------------------------------------------------ peers
    def peer_request(self, app: str, component: str, from_user: str, *, branch: str | None = None,
                     message: str = "") -> PeerRequestView:
        share = PeerShareKind.BRANCH if branch else PeerShareKind.WORKING_TREE
        return self.server.peer_create(PeerRequestCreate(app=app, component=component, to_user=from_user,
                                                         share=share, ref=branch, message=message))

    def peer_respond(self, pr_id: int, approve: bool) -> PeerRequestView:
        pr = next((p for p in self.server.peers() if p.id == pr_id), None)
        if pr is None:
            raise AgentError(f"peer request {pr_id} not found")
        if not approve:
            return self.server.peer_respond(pr_id, False)
        return peers.share(self.cfg, self.server, pr)

    def peer_fetch(self, pr_id: int, component_path: str = ".") -> Path:
        return peers.fetch(self.cfg, self.server, pr_id, component_path)

    # ------------------------------------------------------------------ inbox
    def poll_inbox(self) -> list[InboxEvent]:
        with self._lock:
            events = self.server.inbox(self.cfg.inbox_cursor)
            if events:
                self.cfg.inbox_cursor = events[-1].id
                self.save()
                for e in events:
                    self.notifications.append({"id": e.id, "kind": e.kind, "payload": e.payload,
                                               "created_at": e.created_at.isoformat()})
                self.notifications = self.notifications[-200:]
            return events
