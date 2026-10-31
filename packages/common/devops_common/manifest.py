"""``devops-agent.yaml`` — the per-repository component manifest.

A manifest tells both agents how a repository is structured: which component(s)
it contains, how to build and run them, which files hold configuration, which
environment variables exist and what infrastructure (postgres, redis, ...) they
need. Repos without a manifest get one *inferred* from their structure
(see :func:`infer_manifest`); developers can commit the inferred draft.

Example::

    version: 1
    app: shop
    component:
      name: backend
      kind: backend
      language: python
      framework: fastapi
      port: 8000
      dev:
        command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
        mount: /app
      healthcheck: {path: /health}
      migrations: {command: alembic upgrade head}
      depends_on: [postgres]
      env:
        DATABASE_URL:
          required: true
          local: postgresql+psycopg://app:app@${services.postgres.host}:5432/app
      services:
        postgres:
          image: postgres:16-alpine
          env: {POSTGRES_USER: app, POSTGRES_PASSWORD: app, POSTGRES_DB: app}
          port: 5432
          healthcheck: pg_isready -U app
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from .protocol import ComponentKind

MANIFEST_FILENAMES = ("devops-agent.yaml", "devops-agent.yml", ".devops-agent.yaml")

DEFAULT_DEPENDENCY_FILES = [
    "requirements*.txt",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
]
DEFAULT_BUILD_FILES = ["Dockerfile", "Dockerfile.*", "*.Dockerfile", "docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml", ".dockerignore"]
DEFAULT_MIGRATION_GLOBS = ["alembic/versions/*.py", "*/alembic/versions/*.py", "migrations/versions/*.py", "*/migrations/versions/*.py"]


class BuildSpec(BaseModel):
    dockerfile: str | None = None  # relative to component path; None => agent template
    context: str = "."
    target: str | None = None
    args: dict[str, str] = Field(default_factory=dict)


class DevSpec(BaseModel):
    """How to run the component with hot reload when it is the developer's own code."""

    command: str | None = None
    mount: str | None = "/app"  # container path the source is mounted at
    port: int | None = None  # dev-server port if it differs from ``port``


class HealthSpec(BaseModel):
    path: str | None = None
    command: str | None = None


class MigrationSpec(BaseModel):
    command: str


class EnvVarSpec(BaseModel):
    description: str = ""
    required: bool = False
    secret: bool = False
    default: str | None = None
    # Template used for local test environments, e.g. "http://${components.backend.external_url}".
    local: str | None = None


class ConfigSpec(BaseModel):
    env_files: list[str] = Field(default_factory=lambda: [".env.example", ".env.sample", ".env.template"])
    files: list[str] = Field(default_factory=list)  # globs of config files (yaml/json/ini/ts environments)
    settings_modules: list[str] = Field(default_factory=list)  # pydantic BaseSettings modules


class ServiceDecl(BaseModel):
    image: str
    env: dict[str, str] = Field(default_factory=dict)
    port: int | None = None
    healthcheck: str | None = None
    volumes: list[str] = Field(default_factory=list)


class ComponentManifest(BaseModel):
    name: str
    kind: ComponentKind = ComponentKind.OTHER
    language: str | None = None  # python | javascript | typescript
    framework: str | None = None  # fastapi | flask | react | angular | vite
    path: str = "."  # sub-directory for monorepos
    port: int | None = None
    app_module: str | None = None  # python ASGI/WSGI target, e.g. app.main:app
    build: BuildSpec = Field(default_factory=BuildSpec)
    dev: DevSpec = Field(default_factory=DevSpec)
    healthcheck: HealthSpec = Field(default_factory=HealthSpec)
    migrations: MigrationSpec | None = None
    depends_on: list[str] = Field(default_factory=list)
    env: dict[str, EnvVarSpec] = Field(default_factory=dict)
    config: ConfigSpec = Field(default_factory=ConfigSpec)
    services: dict[str, ServiceDecl] = Field(default_factory=dict)
    dependency_files: list[str] = Field(default_factory=lambda: list(DEFAULT_DEPENDENCY_FILES))
    build_files: list[str] = Field(default_factory=lambda: list(DEFAULT_BUILD_FILES))
    migration_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_MIGRATION_GLOBS))

    @model_validator(mode="before")
    @classmethod
    def _coerce_env(cls, data: Any) -> Any:
        # Allow the short form ``env: {KEY: "default"}`` or ``env: [KEY, ...]``.
        if isinstance(data, dict) and "env" in data:
            env = data["env"]
            if isinstance(env, list):
                data["env"] = {k: {} for k in env}
            elif isinstance(env, dict):
                data["env"] = {
                    k: (v if isinstance(v, dict) else {"default": None if v is None else str(v)})
                    for k, v in env.items()
                }
        return data

    def owns_path(self, repo_relative_path: str) -> bool:
        """True if a repo-relative file path belongs to this component."""
        base = self.path.strip("/").strip(".")
        if not base:
            return True
        return repo_relative_path == base or repo_relative_path.startswith(base + "/")

    def relative(self, repo_relative_path: str) -> str:
        base = self.path.strip("/").strip(".")
        if base and repo_relative_path.startswith(base + "/"):
            return repo_relative_path[len(base) + 1 :]
        return repo_relative_path

    @property
    def container_port(self) -> int | None:
        return self.port

    def dev_port(self) -> int | None:
        return self.dev.port or self.port


class Manifest(BaseModel):
    version: int = 1
    app: str
    components: list[ComponentManifest] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _single_component(cls, data: Any) -> Any:
        if isinstance(data, dict) and "component" in data:
            data = dict(data)
            comp = data.pop("component")
            data.setdefault("components", [])
            data["components"] = [comp, *data["components"]]
        return data

    def get(self, name: str) -> ComponentManifest:
        for c in self.components:
            if c.name == name:
                return c
        raise KeyError(f"component {name!r} not in manifest for app {self.app!r}")

    def component_for_path(self, repo_relative_path: str) -> ComponentManifest | None:
        # Prefer the most specific (longest) path match for monorepos.
        best: ComponentManifest | None = None
        for c in self.components:
            if c.owns_path(repo_relative_path):
                if best is None or len(c.path.strip("./")) > len(best.path.strip("./")):
                    best = c
        return best

    def to_yaml(self) -> str:
        data = self.model_dump(mode="json", exclude_defaults=True)
        return yaml.safe_dump(data, sort_keys=False)


def glob_match(path: str, patterns: list[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch.fnmatch(path, pat) or ("/" not in pat and fnmatch.fnmatch(name, pat)):
            return True
    return False


def parse_manifest(text: str) -> Manifest:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("manifest must be a YAML mapping")
    return Manifest.model_validate(data)


def find_manifest_file(repo_root: Path) -> Path | None:
    for name in MANIFEST_FILENAMES:
        p = repo_root / name
        if p.is_file():
            return p
    return None


def load_manifest(repo_root: Path, *, app: str | None = None, infer: bool = True) -> Manifest | None:
    path = find_manifest_file(repo_root)
    if path is not None:
        return parse_manifest(path.read_text(encoding="utf-8"))
    if infer:
        return infer_manifest(repo_root, app=app)
    return None


# --------------------------------------------------------------------------- #
# Inference — "understand the structure of the codebase" without a manifest
# --------------------------------------------------------------------------- #

_PY_ENTRY_CANDIDATES = [
    ("app/main.py", "app.main:app"),
    ("src/app/main.py", "app.main:app"),
    ("main.py", "main:app"),
    ("app.py", "app:app"),
    ("src/main.py", "main:app"),
    ("wsgi.py", "wsgi:app"),
]


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _python_deps(root: Path) -> str:
    text = ""
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile"):
        text += _read(root / name).lower()
    return text


def infer_component(root: Path, name: str | None = None, path: str = ".") -> ComponentManifest | None:
    """Best-effort component detection from files present in ``root``."""
    comp_name = name or root.resolve().name
    has_dockerfile = (root / "Dockerfile").is_file()
    pkg_json = root / "package.json"

    if pkg_json.is_file():
        try:
            pkg = json.loads(_read(pkg_json) or "{}")
        except json.JSONDecodeError:
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        scripts = pkg.get("scripts", {})
        if "@angular/core" in deps:
            framework, port = "angular", 4200
            dev_cmd = "npx ng serve --host 0.0.0.0 --port 4200"
            files = ["src/environments/*.ts", "angular.json", "proxy.conf.json"]
        elif "vite" in deps or "vite" in scripts.get("dev", ""):
            framework, port = ("react" if "react" in deps else "vite"), 5173
            dev_cmd = "npm run dev -- --host 0.0.0.0 --port 5173"
            files = ["vite.config.*", "src/config.*", "public/config*.json"]
        elif "react-scripts" in deps:
            framework, port = "react", 3000
            dev_cmd = "npm start"
            files = ["src/config.*", "public/config*.json"]
        elif "next" in deps:
            framework, port = "next", 3000
            dev_cmd = "npm run dev -- -H 0.0.0.0 -p 3000"
            files = ["next.config.*"]
        else:
            framework, port = None, 3000
            dev_cmd = "npm run dev" if "dev" in scripts else "npm start"
            files = []
        is_frontend = framework in {"angular", "react", "vite", "next"}
        return ComponentManifest(
            name=comp_name,
            kind=ComponentKind.FRONTEND if is_frontend else ComponentKind.BACKEND,
            language="typescript" if (root / "tsconfig.json").is_file() else "javascript",
            framework=framework,
            path=path,
            port=port,
            build=BuildSpec(dockerfile="Dockerfile" if has_dockerfile else None),
            dev=DevSpec(command=dev_cmd, mount="/app"),
            config=ConfigSpec(files=files),
        )

    py_markers = [root / n for n in ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")]
    if any(p.is_file() for p in py_markers):
        deps = _python_deps(root)
        framework = "fastapi" if "fastapi" in deps else "flask" if "flask" in deps else None
        app_module = None
        for rel, module in _PY_ENTRY_CANDIDATES:
            if (root / rel).is_file():
                app_module = module
                break
        port = 8000 if framework != "flask" else 5000
        if framework == "fastapi":
            dev_cmd = f"uvicorn {app_module or 'main:app'} --host 0.0.0.0 --port {port} --reload"
        elif framework == "flask":
            target = (app_module or "app:app").split(":")[0]
            dev_cmd = f"flask --app {target} run --host 0.0.0.0 --port {port} --debug"
        else:
            dev_cmd = None
        settings_modules = [
            str(p.relative_to(root))
            for p in list(root.glob("**/settings.py"))[:5] + list(root.glob("**/config.py"))[:5]
            if ".venv" not in p.parts and "site-packages" not in p.parts
        ]
        uses_sqlalchemy = "sqlalchemy" in deps
        has_alembic = (root / "alembic.ini").is_file()
        comp = ComponentManifest(
            name=comp_name,
            kind=ComponentKind.BACKEND if framework else ComponentKind.WORKER,
            language="python",
            framework=framework,
            path=path,
            port=port if framework else None,
            app_module=app_module,
            build=BuildSpec(dockerfile="Dockerfile" if has_dockerfile else None),
            dev=DevSpec(command=dev_cmd, mount="/app"),
            healthcheck=HealthSpec(path="/health" if framework else None),
            migrations=MigrationSpec(command="alembic upgrade head") if has_alembic else None,
            config=ConfigSpec(files=["config/*.yaml", "config/*.yml", "config/*.json", "*.ini"], settings_modules=settings_modules),
        )
        if uses_sqlalchemy and ("psycopg" in deps or "asyncpg" in deps or "postgres" in deps):
            comp.depends_on = ["postgres"]
            comp.services = {
                "postgres": ServiceDecl(
                    image="postgres:16-alpine",
                    env={"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "app", "POSTGRES_DB": "app"},
                    port=5432,
                    healthcheck="pg_isready -U app -d app",
                )
            }
            comp.env["DATABASE_URL"] = EnvVarSpec(
                description="SQLAlchemy database URL",
                required=True,
                local="postgresql+psycopg://app:app@${services.postgres.host}:5432/app",
            )
        return comp
    return None


def infer_manifest(repo_root: Path, *, app: str | None = None) -> Manifest | None:
    """Infer a manifest for a single-component repo or a simple monorepo."""
    repo_root = repo_root.resolve()
    app_name = app or repo_root.name
    root_comp = infer_component(repo_root)
    if root_comp is not None:
        return Manifest(app=app_name, components=[root_comp])
    comps: list[ComponentManifest] = []
    for child in sorted(p for p in repo_root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if child.name in {"node_modules", "docs", ".venv", "venv"}:
            continue
        c = infer_component(child, name=child.name, path=child.name)
        if c is not None:
            comps.append(c)
    if not comps:
        return None
    return Manifest(app=app_name, components=comps)


_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_valid_env_key(key: str) -> bool:
    return bool(_ENV_KEY_RE.match(key))
