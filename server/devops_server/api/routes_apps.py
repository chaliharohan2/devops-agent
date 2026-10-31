"""Apps, components, environments, configuration and test-environment specs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from devops_common.gitguard import GitCommandError
from devops_common.manifest import MANIFEST_FILENAMES, parse_manifest
from devops_common.protocol import EnvironmentSpec

from ..db import get_db
from ..models import App, Component, ConfigRevision, Environment
from ..schemas import (
    AppIn,
    AppOut,
    ComponentIn,
    ComponentOut,
    ConfigEntryOut,
    ConfigRevisionOut,
    ConfigUpdate,
    EnvironmentIn,
    EnvironmentOut,
    ImportRepoIn,
)
from ..security import MASK
from ..services import audit, config_store, env_spec, git_service
from .deps import Principal, get_app_or_404, get_env_or_404, get_principal, require

router = APIRouter(tags=["apps"])


def comp_out(c: Component) -> ComponentOut:
    return ComponentOut(id=c.id, name=c.name, kind=c.kind, repo_url=c.repo_url, path=c.path, manifest=c.manifest or {})


def env_out(e: Environment) -> EnvironmentOut:
    return EnvironmentOut(id=e.id, name=e.name, branch=e.branch, branch_overrides=e.branch_overrides or {},
                          requires_approval=e.requires_approval, auto_deploy=e.auto_deploy,
                          deploy_target=e.deploy_target or {}, config_version=e.config_version)


def app_out(a: App) -> AppOut:
    return AppOut(id=a.id, name=a.name, description=a.description,
                  components=[comp_out(c) for c in a.components], environments=[env_out(e) for e in a.environments])


@router.get("/apps", response_model=list[AppOut])
def list_apps(db: Session = Depends(get_db), _: Principal = Depends(get_principal)) -> list[AppOut]:
    return [app_out(a) for a in db.scalars(select(App).order_by(App.name))]


@router.post("/apps", response_model=AppOut, status_code=201)
def create_app(body: AppIn, db: Session = Depends(get_db), p: Principal = Depends(require("maintainer"))) -> AppOut:
    if db.scalar(select(App).where(App.name == body.name)):
        raise HTTPException(409, "app exists")
    app = App(name=body.name, description=body.description)
    db.add(app)
    db.flush()
    for c in body.components:
        _add_component(db, app, c)
    for e in body.environments:
        _add_env(db, app, e)
    audit.record(db, p.name, "app.create", "app", app.id, name=app.name)
    db.commit()
    db.refresh(app)
    return app_out(app)


@router.get("/apps/{app_name}", response_model=AppOut)
def get_app(app_name: str, db: Session = Depends(get_db), _: Principal = Depends(get_principal)) -> AppOut:
    return app_out(get_app_or_404(db, app_name))


def _add_component(db: Session, app: App, c: ComponentIn) -> Component:
    if any(x.name == c.name for x in app.components):
        raise HTTPException(409, f"component {c.name} exists")
    comp = Component(app_id=app.id, name=c.name, kind=c.kind.value, repo_url=c.repo_url, path=c.path,
                     manifest=c.manifest or {})
    db.add(comp)
    app.components.append(comp)
    return comp


def _add_env(db: Session, app: App, e: EnvironmentIn) -> Environment:
    if any(x.name == e.name for x in app.environments):
        raise HTTPException(409, f"environment {e.name} exists")
    env = Environment(app_id=app.id, **e.model_dump())
    db.add(env)
    app.environments.append(env)
    return env


@router.post("/apps/{app_name}/components", response_model=ComponentOut, status_code=201)
def add_component(app_name: str, body: ComponentIn, db: Session = Depends(get_db),
                  p: Principal = Depends(require("maintainer"))) -> ComponentOut:
    app = get_app_or_404(db, app_name)
    comp = _add_component(db, app, body)
    db.flush()
    audit.record(db, p.name, "component.create", "component", comp.id, app=app.name, name=comp.name)
    db.commit()
    return comp_out(comp)


@router.post("/apps/{app_name}/components/import", response_model=list[ComponentOut], status_code=201)
def import_repo(app_name: str, body: ImportRepoIn, db: Session = Depends(get_db),
                p: Principal = Depends(require("maintainer"))) -> list[ComponentOut]:
    """Register every component declared by a repo's ``devops-agent.yaml``."""
    app = get_app_or_404(db, app_name)
    try:
        g = git_service.read_git(body.repo_url, force_sync=True)
        ref = f"refs/remotes/origin/{body.branch}"
        text = next((t for t in (g.show_file(ref, n) for n in MANIFEST_FILENAMES) if t), None)
    except (GitCommandError, OSError) as e:
        raise HTTPException(400, f"cannot read repository: {e}") from e
    if text is None:
        raise HTTPException(400, "repository has no devops-agent.yaml on that branch (run `devops-agent init` locally)")
    manifest = parse_manifest(text)
    created = []
    for cm in manifest.components:
        existing = next((c for c in app.components if c.name == cm.name), None)
        if existing:
            existing.repo_url, existing.path, existing.kind = body.repo_url, cm.path, cm.kind.value
            existing.manifest = cm.model_dump(mode="json")
            created.append(existing)
        else:
            created.append(_add_component(db, app, ComponentIn(name=cm.name, kind=cm.kind, repo_url=body.repo_url,
                                                               path=cm.path, manifest=cm.model_dump(mode="json"))))
    db.flush()
    audit.record(db, p.name, "component.import", "app", app.id, repo=body.repo_url, components=[c.name for c in created])
    db.commit()
    return [comp_out(c) for c in created]


@router.post("/apps/{app_name}/environments", response_model=EnvironmentOut, status_code=201)
def add_environment(app_name: str, body: EnvironmentIn, db: Session = Depends(get_db),
                    p: Principal = Depends(require("maintainer"))) -> EnvironmentOut:
    app = get_app_or_404(db, app_name)
    env = _add_env(db, app, body)
    db.flush()
    audit.record(db, p.name, "environment.create", "environment", env.id, app=app.name, name=env.name)
    db.commit()
    return env_out(env)


@router.patch("/apps/{app_name}/environments/{env_name}", response_model=EnvironmentOut)
def update_environment(app_name: str, env_name: str, body: dict, db: Session = Depends(get_db),
                       p: Principal = Depends(require("maintainer"))) -> EnvironmentOut:
    env = get_env_or_404(get_app_or_404(db, app_name), env_name)
    allowed = {"branch", "branch_overrides", "requires_approval", "auto_deploy", "deploy_target"}
    unknown = set(body) - allowed
    if unknown:
        raise HTTPException(422, f"unknown fields: {sorted(unknown)}")
    for k, v in body.items():
        setattr(env, k, v)
    audit.record(db, p.name, "environment.update", "environment", env.id, fields=sorted(body))
    db.commit()
    return env_out(env)


# ----------------------------------------------------------------------------- configs


@router.get("/apps/{app_name}/environments/{env_name}/configs", response_model=list[ConfigEntryOut], tags=["configs"])
def list_configs(app_name: str, env_name: str, db: Session = Depends(get_db),
                 _: Principal = Depends(get_principal)) -> list[ConfigEntryOut]:
    env = get_env_or_404(get_app_or_404(db, app_name), env_name)
    return [
        ConfigEntryOut(scope=e.scope, key=e.key, value=MASK if e.is_secret else e.value, is_secret=e.is_secret,
                       description=e.description, version=e.version, updated_by=e.updated_by, updated_at=e.updated_at)
        for e in config_store.list_entries(db, env)
    ]


@router.put("/apps/{app_name}/environments/{env_name}/configs", tags=["configs"])
def update_configs(app_name: str, env_name: str, body: ConfigUpdate, db: Session = Depends(get_db),
                   p: Principal = Depends(require("maintainer"))) -> dict:
    env = get_env_or_404(get_app_or_404(db, app_name), env_name)
    try:
        version = config_store.apply(db, env, body.values, actor=p.user.username, message=body.message,
                                     deletes=[(d.component, d.key) for d in body.deletes])
    except config_store.ConfigValidationError as e:
        raise HTTPException(422, str(e)) from e
    db.commit()
    return {"config_version": version}


@router.get("/apps/{app_name}/environments/{env_name}/configs/history", response_model=list[ConfigRevisionOut],
            tags=["configs"])
def config_history(app_name: str, env_name: str, db: Session = Depends(get_db),
                   _: Principal = Depends(get_principal)) -> list[ConfigRevisionOut]:
    env = get_env_or_404(get_app_or_404(db, app_name), env_name)
    rows = db.scalars(select(ConfigRevision).where(ConfigRevision.environment_id == env.id)
                      .order_by(ConfigRevision.version.desc()).limit(200))
    return [ConfigRevisionOut(version=r.version, actor=r.actor, message=r.message, change_request_id=r.change_request_id,
                              changes=r.changes, created_at=r.created_at) for r in rows]


# ----------------------------------------------------------------------------- test env spec


@router.get("/apps/{app_name}/environments/{env_name}/spec", response_model=EnvironmentSpec, tags=["test-environments"])
def environment_spec(app_name: str, env_name: str, db: Session = Depends(get_db),
                     p: Principal = Depends(get_principal)) -> EnvironmentSpec:
    """Everything a local agent needs to recreate this environment in Docker (no secret values)."""
    app = get_app_or_404(db, app_name)
    env = get_env_or_404(app, env_name)
    spec = env_spec.build_spec(db, app, env, include_secrets=False)
    audit.record(db, p.name, "environment.spec", "environment", env.id, app=app.name)
    db.commit()
    return spec
