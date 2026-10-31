"""Render Docker Compose projects from an :class:`EnvironmentSpec`.

Used in two places:

* the **local agent** renders a *test environment*: the developer's own
  component runs from their working copy (mounted, hot reload) while every other
  component is built from a cached clone of the branch the server says the
  environment tracks (or from a peer's shared snapshot);
* the **server agent** renders a *deployment*: every component is built from
  the merged commit with configuration (including secrets) from the config store.

The renderer is pure (no Docker calls) so it is easy to test and to show the
developer exactly what will run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .manifest import ComponentManifest, EnvVarSpec
from .protocol import ComponentRuntimeSpec, EnvironmentSpec, ServiceSpec

PLACEHOLDER_SECRET = "devops-agent-local-placeholder"

_TEMPLATE = re.compile(r"\$\{\s*([a-zA-Z0-9_.\-]+)\s*\}")


@dataclass
class SourcePlan:
    component: str
    path: Path  # on-disk source directory of the component
    mode: Literal["dev", "build"] = "build"
    origin: str = "clone"  # local | clone | peer | release


@dataclass
class RenderOptions:
    project: str
    host_ports: dict[str, int] = field(default_factory=dict)
    purpose: Literal["test", "deploy"] = "test"
    build_network: str | None = None
    build_args: dict[str, str] = field(default_factory=dict)
    publish_services: bool = True
    persist_services: bool = False
    restart: str | None = None
    extra_env: dict[str, dict[str, str]] = field(default_factory=dict)
    image_tag: str = "local"


@dataclass
class RenderResult:
    document: dict[str, Any]
    warnings: list[str]
    stubbed_secrets: dict[str, list[str]]
    urls: dict[str, str]

    def yaml(self) -> str:
        return compose_yaml(self.document)


def compose_yaml(doc: dict[str, Any]) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=120)


def _manifest_of(c: ComponentRuntimeSpec) -> ComponentManifest:
    data = dict(c.manifest or {})
    data.setdefault("name", c.name)
    data.setdefault("kind", c.kind)
    return ComponentManifest.model_validate(data)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-") or "x"


def default_dockerfile(comp: ComponentManifest, purpose: str) -> str | None:
    """A Dockerfile for components that do not ship one (rendered inline)."""
    port = comp.port or 8000
    if comp.language == "python":
        if comp.framework == "flask":
            target = (comp.app_module or "app:app").split(":")[0]
            cmd = f"flask --app {target} run --host 0.0.0.0 --port {port}"
        else:
            cmd = f"uvicorn {comp.app_module or 'main:app'} --host 0.0.0.0 --port {port}"
        return (
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n"
            "COPY . .\n"
            "RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; "
            "elif [ -f pyproject.toml ]; then pip install --no-cache-dir .; fi\n"
            f"EXPOSE {port}\n"
            f"CMD {cmd}\n"
        )
    if comp.language in {"javascript", "typescript"}:
        dev_port = comp.dev_port() or port
        if purpose == "deploy" and comp.framework in {"react", "vite"}:
            cmd = f"sh -c 'npm run build && npx vite preview --host 0.0.0.0 --port {port}'"
        elif comp.framework == "angular":
            cmd = f"npx ng serve --host 0.0.0.0 --port {dev_port}"
        else:
            cmd = comp.dev.command or "npm start"
        return (
            "FROM node:22-alpine\n"
            "WORKDIR /app\n"
            "COPY package*.json ./\n"
            "RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi\n"
            "COPY . .\n"
            f"EXPOSE {dev_port if purpose == 'test' else port}\n"
            f"CMD {cmd}\n"
        )
    return None


def resolve_template(value: str, ctx: dict[str, Any], missing: list[str] | None = None) -> str:
    def lookup(path: str) -> str | None:
        cur: Any = ctx
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return None if isinstance(cur, dict) else str(cur)

    def sub(m: re.Match[str]) -> str:
        v = lookup(m.group(1))
        if v is None:
            if missing is not None:
                missing.append(m.group(1))
            return m.group(0)
        return v

    return _TEMPLATE.sub(sub, value)


def _container_port(comp: ComponentManifest, plan: SourcePlan | None, purpose: str) -> int | None:
    if purpose == "test" and plan is not None and (plan.mode == "dev" or comp.kind == "frontend"):
        return comp.dev_port()
    return comp.port


def build_context(spec: EnvironmentSpec, sources: dict[str, SourcePlan], opts: RenderOptions) -> dict[str, Any]:
    ctx: dict[str, Any] = {"components": {}, "services": {}, "app": spec.app, "environment": spec.environment}
    for c in spec.components:
        m = _manifest_of(c)
        port = _container_port(m, sources.get(c.name), opts.purpose)
        hp = opts.host_ports.get(c.name)
        ctx["components"][c.name] = {
            "host": _slug(c.name),
            "port": port or "",
            "host_port": hp or "",
            "internal_url": f"http://{_slug(c.name)}:{port}" if port else "",
            "external_url": f"http://localhost:{hp}" if hp else "",
        }
    for s in spec.services:
        hp = opts.host_ports.get(s.name)
        ctx["services"][s.name] = {
            "host": _slug(s.name),
            "port": s.port or "",
            "host_port": hp or "",
            "env": dict(s.env),
        }
    return ctx


def _component_env(
    c: ComponentRuntimeSpec, m: ComponentManifest, opts: RenderOptions, ctx: dict[str, Any], warnings: list[str]
) -> tuple[dict[str, str], list[str]]:
    env: dict[str, str] = {}
    stubbed: list[str] = []
    specs: dict[str, EnvVarSpec] = m.env
    for key, s in specs.items():
        if s.default is not None:
            env[key] = s.default
    env.update(c.env)
    if opts.purpose == "test":
        for key, s in specs.items():
            if s.local is not None:
                env[key] = s.local
        for key in c.secret_keys:
            if key not in env:
                env[key] = PLACEHOLDER_SECRET
                stubbed.append(key)
    env.update(opts.extra_env.get(c.name, {}))
    missing: list[str] = []
    resolved = {k: resolve_template(v, ctx, missing) for k, v in env.items()}
    for path in sorted(set(missing)):
        warnings.append(f"{c.name}: unresolved template ${{{path}}}")
    for key, s in specs.items():
        if s.required and key not in resolved:
            warnings.append(f"{c.name}: required env var {key} has no value")
    return resolved, stubbed


def _service_block(s: ServiceSpec, opts: RenderOptions) -> dict[str, Any]:
    block: dict[str, Any] = {"image": s.image}
    if s.env:
        block["environment"] = dict(s.env)
    if s.healthcheck:
        block["healthcheck"] = {
            "test": ["CMD-SHELL", s.healthcheck],
            "interval": "3s",
            "timeout": "3s",
            "retries": 30,
        }
    if s.port and opts.publish_services and s.name in opts.host_ports:
        block["ports"] = [f"127.0.0.1:{opts.host_ports[s.name]}:{s.port}"]
    vols = list(s.volumes)
    if opts.persist_services:
        if s.image.startswith("postgres"):
            vols.append(f"{_slug(s.name)}-data:/var/lib/postgresql/data")
        elif s.image.startswith("redis"):
            vols.append(f"{_slug(s.name)}-data:/data")
    if vols:
        block["volumes"] = vols
    if opts.restart:
        block["restart"] = opts.restart
    block["labels"] = {"devops-agent.kind": "service"}
    return block


def render_compose(spec: EnvironmentSpec, sources: dict[str, SourcePlan], opts: RenderOptions) -> RenderResult:
    warnings: list[str] = []
    stubbed_all: dict[str, list[str]] = {}
    urls: dict[str, str] = {}
    ctx = build_context(spec, sources, opts)
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    service_names = {s.name for s in spec.services}
    healthy_services = {s.name for s in spec.services if s.healthcheck}

    for s in spec.services:
        services[_slug(s.name)] = _service_block(s, opts)
        if opts.persist_services and (s.image.startswith("postgres") or s.image.startswith("redis")):
            volumes[f"{_slug(s.name)}-data"] = {}

    for c in spec.components:
        plan = sources.get(c.name)
        if plan is None:
            warnings.append(f"{c.name}: no source available; component skipped")
            continue
        m = _manifest_of(c)
        name = _slug(c.name)
        build: dict[str, Any] = {"context": str(plan.path)}
        if m.build.dockerfile and (plan.path / m.build.dockerfile).is_file():
            build["dockerfile"] = m.build.dockerfile
        else:
            inline = default_dockerfile(m, opts.purpose)
            if inline is None:
                warnings.append(f"{c.name}: no Dockerfile and no template for language {m.language!r}; skipped")
                continue
            build["dockerfile_inline"] = inline
        if m.build.target:
            build["target"] = m.build.target
        args = {**m.build.args, **opts.build_args}
        if args:
            build["args"] = args
        if opts.build_network:
            build["network"] = opts.build_network

        env, stubbed = _component_env(c, m, opts, ctx, warnings)
        if stubbed:
            stubbed_all[c.name] = stubbed
        block: dict[str, Any] = {
            "build": build,
            "image": f"{_slug(opts.project)}-{name}:{opts.image_tag}",
            "environment": env,
            "labels": {
                "devops-agent.app": spec.app,
                "devops-agent.component": c.name,
                "devops-agent.origin": plan.origin,
                "devops-agent.sha": c.sha or "",
            },
        }
        cport = _container_port(m, plan, opts.purpose)
        if opts.purpose == "test" and (plan.mode == "dev" or m.kind == "frontend") and m.dev.command:
            block["command"] = m.dev.command
        if plan.mode == "dev" and m.dev.mount:
            vols = [f"{plan.path}:{m.dev.mount}"]
            if m.language in {"javascript", "typescript"}:
                vols.append(f"{m.dev.mount.rstrip('/')}/node_modules")  # keep image's node_modules
            block["volumes"] = vols
        if cport and c.name in opts.host_ports:
            hp = opts.host_ports[c.name]
            block["ports"] = [f"{hp}:{cport}"] if opts.purpose == "deploy" else [f"127.0.0.1:{hp}:{cport}"]
            urls[c.name] = f"http://localhost:{hp}"

        depends: dict[str, Any] = {}
        for dep in m.depends_on:
            if dep in service_names:
                depends[_slug(dep)] = {"condition": "service_healthy" if dep in healthy_services else "service_started"}
            elif dep in {x.name for x in spec.components}:
                depends[_slug(dep)] = {"condition": "service_started"}
            else:
                warnings.append(f"{c.name}: depends_on {dep!r} is not part of the environment")
        if m.migrations:
            mig = {
                "build": build,
                "image": block["image"],
                "environment": env,
                "command": m.migrations.command,
                "restart": "no",
                "labels": {**block["labels"], "devops-agent.role": "migration"},
            }
            if depends:
                mig["depends_on"] = dict(depends)
            if "volumes" in block:
                mig["volumes"] = block["volumes"]
            services[f"{name}-migrate"] = mig
            depends[f"{name}-migrate"] = {"condition": "service_completed_successfully"}
        if depends:
            block["depends_on"] = depends
        if opts.restart:
            block["restart"] = opts.restart
        services[name] = block

    doc: dict[str, Any] = {"name": _slug(opts.project), "services": services}
    if volumes:
        doc["volumes"] = volumes
    return RenderResult(document=doc, warnings=warnings, stubbed_secrets=stubbed_all, urls=urls)


def services_from_manifests(manifests: list[ComponentManifest]) -> list[ServiceSpec]:
    """Collect (and de-duplicate by name) infrastructure services declared by components."""
    seen: dict[str, ServiceSpec] = {}
    for m in manifests:
        for name, decl in m.services.items():
            if name in seen:
                continue
            seen[name] = ServiceSpec(
                name=name, image=decl.image, env=dict(decl.env), port=decl.port,
                healthcheck=decl.healthcheck, volumes=list(decl.volumes),
            )
    return list(seen.values())
