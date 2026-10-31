from __future__ import annotations

import textwrap
from pathlib import Path

from devops_common.analysis import analyze_branch, parse_unified_diff
from devops_common.gitguard import ReadOnlyGit
from devops_common.manifest import load_manifest
from devops_common.protocol import ConfigChangeKind as K
from devops_common.testing import commit_and_push


def _changes(report):
    return {(c.kind, c.key): c for c in report.config_changes}


def test_detects_env_vars_settings_deps_and_migrations(backend_repo: tuple[Path, Path]) -> None:
    _, dev = backend_repo
    commit_and_push(
        dev,
        {
            "app/payments.py": 'import os\n\nKEY = os.environ["PAYMENT_API_KEY"]\n',
            "app/settings.py": textwrap.dedent(
                """\
                from pydantic_settings import BaseSettings


                class Settings(BaseSettings):
                    database_url: str
                    redis_url: str = "redis://localhost:6379/0"
                """
            ),
            "app/main.py": textwrap.dedent(
                """\
                from fastapi import FastAPI

                app = FastAPI(title="shop")


                @app.get("/health")
                def health():
                    return {"ok": True}
                """
            ),
            ".env.example": "DATABASE_URL=\nFEATURE_FLAG=true\n",
            "requirements.txt": "fastapi\nsqlalchemy\npsycopg[binary]\nstripe\n",
            "alembic/versions/002_payments.py": "revision = '002'\n",
        },
        "add payments",
        "feature/payments",
    )
    git = ReadOnlyGit(dev)
    manifest = load_manifest(dev)
    report = analyze_branch(
        git, app="shop", component=manifest.get("backend"), base_ref="origin/main",
        head_ref="origin/feature/payments", source_branch="feature/payments", target_branch="main",
    )
    ch = _changes(report)
    pay = ch[(K.ENV_VAR_ADDED, "PAYMENT_API_KEY")]
    assert pay.is_secret_hint and pay.requires_value
    assert pay.description == "Key for the payments provider"  # enriched from manifest
    assert pay.evidence[0].file == "app/payments.py"
    assert (K.ENV_VAR_ADDED, "REDIS_URL") in ch  # pydantic BaseSettings field
    flag = ch[(K.ENV_VAR_ADDED, "FEATURE_FLAG")]
    assert flag.default_value == "true" and not flag.requires_value
    assert (K.ENV_VAR_REMOVED, "APP_NAME") in ch
    assert (K.MIGRATION_ADDED, "alembic/versions/002_payments.py") in ch
    assert (K.DEPENDENCY_CHANGED, "requirements.txt") in ch
    assert (K.CONFIG_FILE_CHANGED, ".env.example") in ch
    assert report.requires_rebuild and report.requires_migration
    assert len(report.commits) == 1
    assert "PAYMENT_API_KEY" in report.summary


def test_no_config_impact(backend_repo: tuple[Path, Path]) -> None:
    _, dev = backend_repo
    commit_and_push(dev, {"app/util.py": "def add(a, b):\n    return a + b\n"}, "util", "feature/util")
    git = ReadOnlyGit(dev)
    manifest = load_manifest(dev)
    report = analyze_branch(
        git, app="shop", component=manifest.get("backend"), base_ref="origin/main",
        head_ref="origin/feature/util", source_branch="feature/util", target_branch="main",
    )
    assert report.config_changes == []
    assert not report.requires_rebuild
    assert "No configuration impact" in report.summary


def test_moved_env_var_is_not_reported(backend_repo: tuple[Path, Path]) -> None:
    _, dev = backend_repo
    commit_and_push(
        dev,
        {
            "app/main.py": "from fastapi import FastAPI\nfrom app.conf import NAME\napp = FastAPI(title=NAME)\n",
            "app/conf.py": 'import os\nNAME = os.getenv("APP_NAME", "shop")\n',
        },
        "move",
        "feature/move",
    )
    report = analyze_branch(
        ReadOnlyGit(dev), app="shop", component=load_manifest(dev).get("backend"), base_ref="origin/main",
        head_ref="origin/feature/move", source_branch="feature/move", target_branch="main",
    )
    keys = {(c.kind, c.key) for c in report.config_changes}
    assert (K.ENV_VAR_REMOVED, "APP_NAME") not in keys
    assert (K.ENV_VAR_ADDED, "APP_NAME") not in keys


def test_secret_warning(backend_repo: tuple[Path, Path]) -> None:
    _, dev = backend_repo
    commit_and_push(dev, {"app/aws.py": 'AWS = "AKIAABCDEFGHIJKLMNOP"\n'}, "oops", "feature/oops")
    report = analyze_branch(
        ReadOnlyGit(dev), app="shop", component=load_manifest(dev).get("backend"), base_ref="origin/main",
        head_ref="origin/feature/oops", source_branch="feature/oops", target_branch="main",
    )
    assert any("AWS access key" in w for w in report.warnings)


def test_parse_unified_diff_line_numbers() -> None:
    diff = textwrap.dedent(
        """\
        diff --git a/x.py b/x.py
        --- a/x.py
        +++ b/x.py
        @@ -3,0 +4,2 @@
        +a = 1
        +b = 2
        @@ -10 +12 @@
        -old
        +new
        """
    )
    files = parse_unified_diff(diff)
    assert [(ln.line, ln.text) for ln in files["x.py"].added] == [(4, "a = 1"), (5, "b = 2"), (12, "new")]
    assert [(ln.line, ln.text) for ln in files["x.py"].removed] == [(10, "old")]
