"""Deployment executors.

``compose``  — export each component's merged commit (``git archive``), render a
               Compose project with config from the store and run
               ``docker compose up -d --build`` on the server (or ``DOCKER_HOST``).
``dry_run``  — render and record the plan without running anything (useful for
               CI, demos, and environments managed by another system).

Kubernetes/Helm and cloud targets plug in here later (see docs/PLAN.md).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from devops_common.compose import RenderOptions, SourcePlan, render_compose
from devops_common.gitguard import GitCommandError

from ..models import App, Deployment, Environment
from ..security import MASK
from ..settings import get_settings
from . import audit, config_store, env_spec, git_service, llm_service

log = logging.getLogger(__name__)


def _mask_plan(doc: dict[str, Any], secret_keys: dict[str, set[str]]) -> dict[str, Any]:
    import copy

    masked = copy.deepcopy(doc)
    for name, svc in masked.get("services", {}).items():
        comp = (svc.get("labels") or {}).get("devops-agent.component")
        keys = secret_keys.get(comp or name, set())
        env = svc.get("environment") or {}
        for k in list(env):
            if k in keys:
                env[k] = MASK
    return masked


def _append(dep: Deployment, line: str) -> None:
    dep.log = (dep.log or "") + line.rstrip() + "\n"


def run_deployment(db: Session, dep: Deployment) -> None:
    s = get_settings()
    env: Environment = dep.environment
    app: App = dep.app
    dep.status = "running"
    dep.started_at = datetime.now(UTC)
    target = (env.deploy_target or {}).get("type") or s.default_deploy_target
    dep.target = target
    db.flush()
    try:
        spec = env_spec.build_spec(db, app, env, include_secrets=True, shas=dep.component_shas or None)
        dep.component_shas = {c.name: c.sha for c in spec.components if c.sha}
        dep.config_version = spec.config_version
        work = s.deploy_dir / app.name / env.name
        sources: dict[str, SourcePlan] = {}
        for c in spec.components:
            if not (c.repo_url and c.sha):
                _append(dep, f"[skip] {c.name}: no commit to deploy")
                continue
            comp_row = next(x for x in app.components if x.name == c.name)
            dest = work / "src" / f"{c.name}-{c.sha[:12]}"
            if not dest.exists():
                g = git_service.read_git(c.repo_url)
                tmp = dest.with_suffix(".tmp")
                shutil.rmtree(tmp, ignore_errors=True)
                root = g.export_tree(c.sha, tmp, comp_row.path)
                if root != tmp:
                    shutil.move(str(root), str(dest))
                    shutil.rmtree(tmp, ignore_errors=True)
                else:
                    tmp.rename(dest)
            sources[c.name] = SourcePlan(c.name, dest.resolve(), mode="build", origin="release")
            _append(dep, f"[source] {c.name} @ {c.sha[:12]} ({c.ref})")
        host_ports = {k: int(v) for k, v in ((env.deploy_target or {}).get("host_ports") or {}).items()}
        project = f"{app.name}-{env.name}"
        res = render_compose(
            spec, sources,
            RenderOptions(project=project, purpose="deploy", host_ports=host_ports, restart="unless-stopped",
                          persist_services=True, publish_services=False, build_network=s.deploy_build_network,
                          image_tag=f"cfg{spec.config_version}"),
        )
        for w in res.warnings:
            _append(dep, f"[warn] {w}")
        secret_keys = {
            c.name: {e.key for e in config_store.list_entries(db, env) if e.is_secret and e.scope in ("*", c.name)}
            for c in spec.components
        }
        dep.plan = {"compose": _mask_plan(res.document, secret_keys), "urls": res.urls, "warnings": res.warnings}
        work.mkdir(parents=True, exist_ok=True)
        compose_file = work / "docker-compose.yml"
        compose_file.write_text(res.yaml(), encoding="utf-8")
        os.chmod(compose_file, 0o600)  # contains decrypted secrets
        if target == "dry_run":
            _append(dep, f"[dry-run] rendered {compose_file} ({len(res.document['services'])} services); not executed")
            dep.status = "succeeded"
        else:
            cmd = [s.docker_bin, "compose", "-p", project, "-f", str(compose_file), "up", "-d", "--build",
                   "--remove-orphans", "--wait", "--wait-timeout", "300"]
            _append(dep, "$ " + " ".join(cmd))
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            _append(dep, proc.stdout[-20000:])
            _append(dep, proc.stderr[-20000:])
            dep.status = "succeeded" if proc.returncode == 0 else "failed"
            if proc.returncode != 0:
                dep.diagnosis = llm_service.diagnose_deployment(dep.log)
    except (GitCommandError, OSError, subprocess.SubprocessError, ValueError, StopIteration) as e:
        log.exception("deployment %s failed", dep.id)
        _append(dep, f"[error] {type(e).__name__}: {e}")
        dep.status = "failed"
        dep.diagnosis = llm_service.diagnose_deployment(dep.log)
    dep.finished_at = datetime.now(UTC)
    audit.record(db, audit.SERVER_AGENT, f"deployment.{dep.status}", "deployment", dep.id,
                 app=app.name, environment=env.name, target=target, shas=dep.component_shas)
    db.flush()


def deploy_dir_for(app: str, env: str) -> Path:
    return get_settings().deploy_dir / app / env
