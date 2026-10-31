"""Build end-to-end test environments in Docker.

Given an app + environment (e.g. ``shop`` / ``dev``) and the component(s) the
developer is working on, the builder:

1. asks the server agent for the environment spec (components, tracked
   branches/commits, non-secret config, infrastructure services);
2. resolves a source for every component:
   * **local**  – the developer's own checkout, mounted into the container with
     hot reload (uncommitted changes included — that is the point of testing);
   * **peer**   – a teammate's shared branch/working tree (phase 2);
   * **clone**  – everything else, exported from an agent-owned cache clone at
     the exact commit the environment runs (``git archive``; never a checkout of
     the developer's repos);
3. allocates free host ports, renders ``docker-compose.yml`` and starts it;
4. waits for health checks and tells the developer exactly how to test.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from devops_common.compose import RenderOptions, RenderResult, SourcePlan, render_compose
from devops_common.gitguard import WorkspaceGit
from devops_common.manifest import ComponentManifest
from devops_common.protocol import ComponentRuntimeSpec, EnvironmentSpec

from .config import LocalConfig
from .errors import AgentError
from .server_client import ServerClient

DockerRunner = Callable[[list[str], Path], subprocess.CompletedProcess]


def default_runner(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=3600)


class EnvError(AgentError):
    pass


@dataclass
class ComponentSource:
    path: str
    mode: str
    origin: str
    ref: str | None = None
    sha: str | None = None


@dataclass
class EnvState:
    name: str
    app: str
    environment: str
    project: str
    local_components: list[str]
    status: str = "preparing"  # preparing | building | running | failed | stopped
    ports: dict[str, int] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)
    sources: dict[str, dict] = field(default_factory=dict)
    services: list[str] = field(default_factory=list)
    stubbed_secrets: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    health: dict[str, str] = field(default_factory=dict)
    message: str = ""
    instructions: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


def _slug(s: str) -> str:
    import re

    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class EnvBuilder:
    def __init__(self, cfg: LocalConfig, server: ServerClient, runner: DockerRunner | None = None):
        self.cfg = cfg
        self.server = server
        self.run = runner or default_runner

    # ------------------------------------------------------------------ state
    def _dir(self, name: str) -> Path:
        return self.cfg.envs_dir / name

    def save(self, st: EnvState) -> None:
        st.updated_at = datetime.now(UTC).isoformat()
        d = self._dir(st.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps(st.to_dict(), indent=2), encoding="utf-8")

    def get(self, name: str) -> EnvState:
        p = self._dir(name) / "state.json"
        if not p.is_file():
            raise EnvError(f"no environment named {name!r}")
        return EnvState(**json.loads(p.read_text(encoding="utf-8")))

    def list(self) -> list[EnvState]:
        out = []
        if self.cfg.envs_dir.is_dir():
            for p in sorted(self.cfg.envs_dir.glob("*/state.json")):
                try:
                    out.append(EnvState(**json.loads(p.read_text(encoding="utf-8"))))
                except (ValueError, TypeError):
                    continue
        return out

    # ---------------------------------------------------------------- sources
    def _clone_source(self, app: str, comp: ComponentRuntimeSpec, manifest: ComponentManifest) -> ComponentSource:
        if not comp.repo_url:
            raise EnvError(f"component {comp.name} has no repository configured on the server")
        root = self.cfg.workspace_dir
        cache = root / _slug(app) / _slug(comp.name) / "repo"
        g = WorkspaceGit(cache, root)
        if not (cache / ".git").exists():
            g.clone(comp.repo_url)
        g.fetch("origin", "+refs/heads/*:refs/remotes/origin/*")
        sha = comp.sha or g.rev_parse(f"refs/remotes/origin/{comp.ref}")
        dest = root / _slug(app) / _slug(comp.name) / f"src-{sha[:12]}"
        if not dest.exists():
            tmp = dest.with_name(dest.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            exported = g.export_tree(sha, tmp, manifest.path)
            if exported != tmp:
                shutil.move(str(exported), str(dest))
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                tmp.rename(dest)
        return ComponentSource(path=str(dest), mode="build", origin="clone", ref=comp.ref, sha=sha)

    def _local_source(self, app: str, comp: ComponentRuntimeSpec, manifest: ComponentManifest) -> ComponentSource:
        repo = self.cfg.repo_for(app, comp.name)
        if repo is None or not repo.is_dir():
            raise EnvError(f"{app}/{comp.name} is not linked to a local checkout. "
                           f"Run: devops-agent link {app}/{comp.name} /path/to/checkout")
        base = manifest.path.strip("/").strip(".")
        path = (repo / base) if base else repo
        return ComponentSource(path=str(path.resolve()), mode="dev", origin="local")

    def _allocate_ports(self, names: list[str], keep: dict[str, int], env_name: str) -> dict[str, int]:
        """Stable ports per environment: reuse this env's previous ports, avoid other envs' ports."""
        used = {p for st in self.list() if st.name != env_name and st.status != "stopped" for p in st.ports.values()}
        ports: dict[str, int] = {}
        candidate = self.cfg.port_range_start
        for n in names:
            if n in keep and keep[n] not in used:
                ports[n] = keep[n]
                continue
            while candidate in used or candidate in ports.values() or not _port_free(candidate):
                candidate += 1
            ports[n] = candidate
            candidate += 1
        return ports

    # ----------------------------------------------------------------- render
    def prepare(self, app: str, environment: str, local_components: list[str],
                peer_sources: dict[str, Path] | None = None, name: str | None = None) -> tuple[EnvState, RenderResult]:
        spec: EnvironmentSpec = self.server.spec(app, environment)
        names = {c.name for c in spec.components}
        unknown = set(local_components) - names
        if unknown:
            raise EnvError(f"unknown component(s) {sorted(unknown)}; {app} has {sorted(names)}")
        name = name or _slug(f"{app}-{environment}-{'-'.join(sorted(local_components)) or 'baseline'}")
        previous = None
        try:
            previous = self.get(name)
        except EnvError:
            pass
        st = EnvState(name=name, app=app, environment=environment, project=f"dxa-{name}",
                      local_components=sorted(local_components))
        sources: dict[str, SourcePlan] = {}
        for comp in spec.components:
            manifest = ComponentManifest.model_validate({**comp.manifest, "name": comp.name})
            if comp.name in local_components:
                src = self._local_source(app, comp, manifest)
            elif peer_sources and comp.name in peer_sources:
                src = ComponentSource(path=str(Path(peer_sources[comp.name]).resolve()), mode="build", origin="peer")
            else:
                src = self._clone_source(app, comp, manifest)
            st.sources[comp.name] = asdict(src)
            mode = "dev" if src.mode == "dev" else "build"
            sources[comp.name] = SourcePlan(comp.name, Path(src.path), mode=mode, origin=src.origin)
        port_names = [c.name for c in spec.components if c.manifest.get("port") or c.manifest.get("dev", {}).get("port")]
        port_names += [s.name for s in spec.services if s.port]
        st.ports = self._allocate_ports(port_names, previous.ports if previous else {}, name)
        res = render_compose(
            spec, sources,
            RenderOptions(project=st.project, host_ports=st.ports, purpose="test",
                          build_network=self.cfg.build_network, image_tag="test"),
        )
        st.urls = res.urls
        st.services = list(res.document["services"])
        st.stubbed_secrets = res.stubbed_secrets
        st.warnings = res.warnings + spec.notes
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "docker-compose.yml").write_text(res.yaml(), encoding="utf-8")
        (d / "spec.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        st.instructions = self.instructions(st, spec)
        self.save(st)
        return st, res

    # -------------------------------------------------------------- lifecycle
    def compose_cmd(self, st: EnvState, *args: str) -> list[str]:
        return [self.cfg.docker_bin, "compose", "-p", st.project, "-f", str(self._dir(st.name) / "docker-compose.yml"), *args]

    def up(self, st: EnvState, *, wait_s: float = 240.0, log: Callable[[str], None] | None = None) -> EnvState:
        st.status = "building"
        st.message = "building images and starting containers"
        self.save(st)
        proc = self.run(self.compose_cmd(st, "up", "-d", "--build", "--remove-orphans"), self._dir(st.name))
        (self._dir(st.name) / "last-up.log").write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
        if proc.returncode != 0:
            st.status = "failed"
            st.message = (proc.stderr or proc.stdout or "docker compose failed")[-3000:]
            self.save(st)
            return st
        st.health = self.wait_healthy(st, wait_s, log=log)
        bad = {k: v for k, v in st.health.items() if v != "healthy"}
        st.status = "running" if not bad else "failed"
        st.message = "all components healthy" if not bad else f"not healthy: {bad}"
        self.save(st)
        return st

    def wait_healthy(self, st: EnvState, timeout_s: float, log: Callable[[str], None] | None = None) -> dict[str, str]:
        spec = EnvironmentSpec.model_validate_json((self._dir(st.name) / "spec.json").read_text(encoding="utf-8"))
        checks: dict[str, str] = {}
        for comp in spec.components:
            if comp.name not in st.urls:
                continue
            path = (comp.manifest.get("healthcheck") or {}).get("path") or "/"
            checks[comp.name] = st.urls[comp.name].rstrip("/") + path
        status = {k: "starting" for k in checks}
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and any(v != "healthy" for v in status.values()):
            for k, url in checks.items():
                if status[k] == "healthy":
                    continue
                try:
                    if httpx.get(url, timeout=3).status_code < 500:
                        status[k] = "healthy"
                        if log:
                            log(f"{k} is healthy at {url}")
                except httpx.HTTPError:
                    pass
            if any(v != "healthy" for v in status.values()):
                time.sleep(2)
        return {k: ("healthy" if v == "healthy" else "unhealthy") for k, v in status.items()}

    def down(self, name: str, *, volumes: bool = True) -> EnvState:
        st = self.get(name)
        args = ["down", "--remove-orphans"] + (["-v"] if volumes else [])
        proc = self.run(self.compose_cmd(st, *args), self._dir(name))
        st.status = "stopped" if proc.returncode == 0 else st.status
        st.message = "stopped" if proc.returncode == 0 else (proc.stderr or "")[-2000:]
        self.save(st)
        return st

    def remove(self, name: str) -> None:
        try:
            self.down(name)
        finally:
            shutil.rmtree(self._dir(name), ignore_errors=True)

    def logs(self, name: str, service: str | None = None, tail: int = 200) -> str:
        st = self.get(name)
        proc = self.run(self.compose_cmd(st, "logs", "--no-color", "--tail", str(tail), *([service] if service else [])),
                        self._dir(name))
        return (proc.stdout or "") + (proc.stderr or "")

    # ----------------------------------------------------------- instructions
    def instructions(self, st: EnvState, spec: EnvironmentSpec) -> str:
        lines = [f'Test environment "{st.name}" ({st.app} / {st.environment})', ""]
        mine = [c for c in spec.components if st.sources.get(c.name, {}).get("origin") == "local"]
        peers = [c for c in spec.components if st.sources.get(c.name, {}).get("origin") == "peer"]
        others = [c for c in spec.components if st.sources.get(c.name, {}).get("origin") == "clone"]
        if mine:
            lines.append("Your code (mounted, live reload):")
            for c in mine:
                lines.append(f"  - {c.name}: {st.urls.get(c.name, '(no port)')}  <- {st.sources[c.name]['path']}")
        if peers:
            lines.append("Teammate code (peer share):")
            for c in peers:
                lines.append(f"  - {c.name}: {st.urls.get(c.name, '(no port)')}")
        if others:
            lines.append(f"From the server ({st.environment} environment):")
            for c in others:
                sha = (st.sources[c.name].get("sha") or "")[:10]
                lines.append(f"  - {c.name} @ {c.ref} ({sha}): {st.urls.get(c.name, '(no port)')}")
        if spec.services:
            lines.append("Infrastructure:")
            for s in spec.services:
                port = st.ports.get(s.name)
                creds = ", ".join(f"{k}={v}" for k, v in s.env.items() if "PASSWORD" in k or "USER" in k)
                lines.append(f"  - {s.name} ({s.image}): localhost:{port}" + (f"  [{creds}]" if creds else ""))
        fe = next((c for c in spec.components if c.kind == "frontend" and c.name in st.urls), None)
        lines += ["", "How to test end to end:"]
        step = 1
        if fe:
            lines.append(f"  {step}. Open {st.urls[fe.name]} in your browser.")
            step += 1
        for c in mine:
            lines.append(f"  {step}. Edit {c.name} in {st.sources[c.name]['path']} — changes reload automatically.")
            step += 1
            if c.manifest.get("migrations"):
                lines.append(f"  {step}. New migration? docker compose -p {st.project} -f "
                             f"{self._dir(st.name) / 'docker-compose.yml'} run --rm {_slug(c.name)}-migrate")
                step += 1
        lines.append(f"  {step}. Logs: devops-agent env logs {st.name} [service]")
        lines.append(f"  {step + 1}. Rebuild after dependency changes: devops-agent env up {st.app} --env "
                     f"{st.environment} " + " ".join(f"--local {c}" for c in st.local_components))
        lines.append(f"  {step + 2}. Tear down: devops-agent env down {st.name}")
        stub = [f"{c}: {', '.join(keys)}" for c, keys in st.stubbed_secrets.items()]
        if stub:
            lines += ["", "Secrets are never copied to laptops; these use placeholders: " + "; ".join(stub)]
        if st.warnings:
            lines += ["", "Notes:"] + [f"  - {w}" for w in st.warnings]
        return "\n".join(lines)
