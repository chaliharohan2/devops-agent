"""Deterministic change analysis.

Given a repository, a target ref and a pushed branch, work out *what the change
means operationally*:

* environment variables introduced or no longer used (Python ``os.environ`` /
  ``os.getenv`` / pydantic ``BaseSettings`` fields, JS ``process.env`` /
  ``import.meta.env``, Dockerfile ``ENV``/``ARG``, ``.env.example`` entries)
* config files that changed (manifest ``config.files`` globs)
* dependency manifests that changed (=> image rebuild)
* new database migrations (=> run migrations before rollout)
* build/runtime definition changes (Dockerfile, compose files)
* possible secrets accidentally committed (warning only)

Both agents run this same code: the local agent to *propose* config changes and
the server agent to *verify* them independently before merging. The LLMs only
ever summarise or enrich these facts; they are never the source of truth.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from .gitguard import _GuardedGit
from .manifest import ComponentManifest, Manifest, glob_match
from .protocol import ChangeReport, CommitInfo, ConfigChange, ConfigChangeKind, Evidence, FileChange

ANALYZER_VERSION = "1"

_Q = r"""['"]"""
_VAR = r"([A-Z][A-Z0-9_]{1,})"

PY_ENV_PATTERNS = [
    re.compile(rf"os\.environ\[\s*{_Q}{_VAR}{_Q}\s*\]"),
    re.compile(rf"(?:os\.)?environ\.get\(\s*{_Q}{_VAR}{_Q}"),
    re.compile(rf"os\.getenv\(\s*{_Q}{_VAR}{_Q}"),
    re.compile(rf"(?:validation_alias|env)\s*=\s*{_Q}{_VAR}{_Q}"),
    re.compile(rf"config\(\s*{_Q}{_VAR}{_Q}"),  # python-decouple
]
JS_ENV_PATTERNS = [
    re.compile(rf"process\.env\.{_VAR}"),
    re.compile(rf"process\.env\[\s*{_Q}{_VAR}{_Q}\s*\]"),
    re.compile(rf"import\.meta\.env\.{_VAR}"),
]
DOCKER_ENV_PATTERNS = [re.compile(rf"^\s*(?:ENV|ARG)\s+{_VAR}")]
COMPOSE_ENV_PATTERNS = [re.compile(rf"^\s*-?\s*{_VAR}\s*[:=]"), re.compile(rf"\$\{{{_VAR}(?::?-[^}}]*)?\}}")]
ENV_FILE_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")

PY_EXT = (".py",)
JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")

# Variables provided by runtimes/tooling that are never application config.
BUILTIN_VARS = {
    "PATH", "HOME", "PWD", "USER", "HOSTNAME", "SHELL", "LANG", "TERM", "TZ", "CI",
    "NODE_ENV", "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PORT",
    "MODE", "BASE_URL", "PROD", "DEV", "SSR", "PIP_NO_CACHE_DIR", "DEBIAN_FRONTEND",
}

SECRET_NAME_HINT = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|API_KEY|ACCESS_KEY|_KEY$|DSN)")
SECRET_VALUE_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)(?:password|passwd|secret|api_key|token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"), "hard-coded credential"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "API secret key"),
]


@dataclass
class _Line:
    file: str
    line: int | None
    text: str


@dataclass
class _FileDiff:
    path: str
    added: list[_Line] = field(default_factory=list)
    removed: list[_Line] = field(default_factory=list)


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_unified_diff(text: str) -> dict[str, _FileDiff]:
    files: dict[str, _FileDiff] = {}
    cur: _FileDiff | None = None
    old_path: str | None = None
    new_ln = old_ln = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            cur = None
            old_path = None
            continue
        if raw.startswith("--- "):
            old_path = raw[4:].removeprefix("a/") if raw[4:] != "/dev/null" else None
            continue
        if raw.startswith("+++ "):
            new = raw[4:]
            path = new.removeprefix("b/") if new != "/dev/null" else old_path
            if path is None:
                cur = None
                continue
            cur = files.setdefault(path, _FileDiff(path))
            continue
        m = _HUNK.match(raw)
        if m:
            old_ln = int(m.group(1))
            new_ln = int(m.group(3))
            continue
        if cur is None:
            continue
        if raw.startswith("+"):
            cur.added.append(_Line(cur.path, new_ln, raw[1:]))
            new_ln += 1
        elif raw.startswith("-"):
            cur.removed.append(_Line(cur.path, old_ln, raw[1:]))
            old_ln += 1
    return files


def _env_patterns_for(path: str) -> list[re.Pattern[str]]:
    name = path.rsplit("/", 1)[-1]
    if path.endswith(PY_EXT):
        return PY_ENV_PATTERNS
    if path.endswith(JS_EXT):
        return JS_ENV_PATTERNS
    if name.startswith("Dockerfile") or name.endswith(".Dockerfile"):
        return DOCKER_ENV_PATTERNS
    return []


def _settings_env_vars(source: str | None) -> dict[str, int]:
    """Env var names declared by pydantic ``BaseSettings`` classes in a module."""
    if not source or "BaseSettings" not in source:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {getattr(b, "id", None) or getattr(b, "attr", None) for b in node.bases}
        if "BaseSettings" not in bases:
            continue
        prefix = ""
        for stmt in node.body:
            # model_config = SettingsConfigDict(env_prefix="APP_")
            if isinstance(stmt, ast.Assign) and any(getattr(t, "id", "") == "model_config" for t in stmt.targets):
                if isinstance(stmt.value, ast.Call):
                    for kw in stmt.value.keywords:
                        if kw.arg == "env_prefix" and isinstance(kw.value, ast.Constant):
                            prefix = str(kw.value.value)
            # class Config: env_prefix = "APP_"
            if isinstance(stmt, ast.ClassDef) and stmt.name == "Config":
                for s in stmt.body:
                    if (
                        isinstance(s, ast.Assign)
                        and any(getattr(t, "id", "") == "env_prefix" for t in s.targets)
                        and isinstance(s.value, ast.Constant)
                    ):
                        prefix = str(s.value.value)
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
                if name.startswith("_") or name == "model_config":
                    continue
                out[(prefix + name).upper()] = stmt.lineno
    return out


def _secret_hint(key: str) -> bool:
    return bool(SECRET_NAME_HINT.search(key))


class _Collector:
    def __init__(self, component: str):
        self.component = component
        self.items: dict[tuple[str, str | None], ConfigChange] = {}

    def add(self, kind: ConfigChangeKind, key: str | None, evidence: Evidence | None = None, **kw) -> ConfigChange:
        k = (kind.value, key)
        if k not in self.items:
            self.items[k] = ConfigChange(kind=kind, component=self.component, key=key, **kw)
        item = self.items[k]
        if evidence and len(item.evidence) < 5:
            item.evidence.append(evidence)
        for attr, val in kw.items():
            if val and not getattr(item, attr):
                setattr(item, attr, val)
        return item

    def result(self) -> list[ConfigChange]:
        order = list(ConfigChangeKind)
        return sorted(self.items.values(), key=lambda c: (order.index(c.kind), c.key or ""))


def analyze_branch(
    git: _GuardedGit,
    *,
    app: str,
    component: ComponentManifest,
    base_ref: str,
    head_ref: str,
    source_branch: str,
    target_branch: str,
    repo_url: str | None = None,
    manifest: Manifest | None = None,
) -> ChangeReport:
    """Analyze ``head_ref`` against ``base_ref`` for a single component."""
    head_sha = git.rev_parse(head_ref)
    target_sha = git.rev_parse(base_ref)
    base_sha = git.merge_base(target_sha, head_sha)
    warnings: list[str] = []

    # ---- commits -----------------------------------------------------------
    log = git.run("log", "--no-color", "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s", f"{base_sha}..{head_sha}").stdout
    commits = []
    for ln in log.splitlines():
        parts = ln.split("\x1f")
        if len(parts) == 5:
            commits.append(CommitInfo(sha=parts[0], author=parts[1], email=parts[2], date=parts[3], subject=parts[4]))
    if not commits:
        warnings.append(f"{source_branch} has no commits that are not already in {target_branch}.")
    behind = git.out("rev-list", "--count", f"{head_sha}..{target_sha}")
    if behind and behind != "0":
        warnings.append(f"{target_branch} has {behind} commit(s) not in {source_branch}; the merge will combine them.")

    # ---- changed files -----------------------------------------------------
    numstat = git.run("diff", "--no-color", "--no-ext-diff", "--numstat", "--no-renames", base_sha, head_sha).stdout
    stats: dict[str, tuple[int, int]] = {}
    for ln in numstat.splitlines():
        a, d, p = (ln.split("\t", 2) + ["", "", ""])[:3]
        stats[p] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    name_status = git.run("diff", "--no-color", "--no-ext-diff", "--name-status", "--no-renames", base_sha, head_sha).stdout
    files: list[FileChange] = []
    for ln in name_status.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0][0], parts[-1]
        if not component.owns_path(path):
            continue
        a, d = stats.get(path, (0, 0))
        files.append(FileChange(path=path, status=status, additions=a, deletions=d))

    col = _Collector(component.name)
    if not files:
        return ChangeReport(
            app=app, component=component.name, repo_url=repo_url, source_branch=source_branch,
            target_branch=target_branch, base_sha=base_sha, head_sha=head_sha, commits=commits,
            warnings=warnings + [f"No files of component {component.name!r} changed."],
            analyzer_version=ANALYZER_VERSION,
        )

    comp_scope = component.path.strip("/").strip(".") or "."
    diffs = parse_unified_diff(
        git.run(
            "diff", "--no-color", "--no-ext-diff", "-U0", "--no-renames", base_sha, head_sha, "--",
            *[f.path for f in files],
        ).stdout
    )

    requires_rebuild = False
    requires_migration = False
    declared = component.env

    for f in files:
        rel = component.relative(f.path)
        name = rel.rsplit("/", 1)[-1]
        fd = diffs.get(f.path, _FileDiff(f.path))

        # ---- structural categories -----------------------------------------
        if name in {"devops-agent.yaml", "devops-agent.yml", ".devops-agent.yaml"}:
            col.add(ConfigChangeKind.MANIFEST_CHANGED, rel, Evidence(file=f.path), description="Component manifest changed")
        if glob_match(rel, component.dependency_files):
            requires_rebuild = True
            col.add(ConfigChangeKind.DEPENDENCY_CHANGED, rel, Evidence(file=f.path),
                    description="Dependencies changed; images must be rebuilt")
        if glob_match(rel, component.build_files):
            requires_rebuild = True
            col.add(ConfigChangeKind.BUILD_CHANGED, rel, Evidence(file=f.path),
                    description="Build/runtime definition changed")
        if glob_match(rel, component.migration_globs):
            if f.status == "A":
                requires_migration = True
                col.add(ConfigChangeKind.MIGRATION_ADDED, rel, Evidence(file=f.path),
                        description="New database migration; run migrations before rollout")
            elif f.status == "M":
                warnings.append(f"Existing migration {f.path} was modified; environments that already applied it will not re-run it.")
            elif f.status == "D":
                warnings.append(f"Migration {f.path} was deleted; this can break the migration chain.")
        is_env_file = glob_match(rel, component.config.env_files)
        if is_env_file or glob_match(rel, component.config.files):
            col.add(ConfigChangeKind.CONFIG_FILE_CHANGED, rel, Evidence(file=f.path),
                    description="Environment template changed" if is_env_file else "Configuration file changed")

        # ---- secrets accidentally committed ---------------------------------
        for ln in fd.added:
            for pat, label in SECRET_VALUE_PATTERNS:
                if pat.search(ln.text) and not is_env_file:
                    warnings.append(f"Possible {label} committed in {f.path}:{ln.line}. Move it to the config store.")
                    break

        # ---- env files (.env.example) ---------------------------------------
        if is_env_file:
            base_keys = {
                m.group(1)
                for m in (ENV_FILE_LINE.match(x) for x in (git.show_file(base_sha, f.path) or "").splitlines())
                if m
            }
            head_text = git.show_file(head_sha, f.path) or ""
            head_kv = {m.group(1): m.group(2) for m in (ENV_FILE_LINE.match(x) for x in head_text.splitlines()) if m}
            for ln in fd.added:
                m = ENV_FILE_LINE.match(ln.text)
                if m and m.group(1) not in base_keys:
                    key, default = m.group(1), m.group(2).strip("'\"") or None
                    col.add(ConfigChangeKind.ENV_VAR_ADDED, key, Evidence(file=f.path, line=ln.line, snippet=ln.text.strip()[:200]),
                            default_value=default, is_secret_hint=_secret_hint(key))
            for ln in fd.removed:
                m = ENV_FILE_LINE.match(ln.text)
                if m and m.group(1) not in head_kv:
                    col.add(ConfigChangeKind.ENV_VAR_REMOVED, m.group(1),
                            Evidence(file=f.path, line=ln.line, snippet=ln.text.strip()[:200]))
            continue

        # ---- pydantic BaseSettings ------------------------------------------
        if f.path.endswith(".py"):
            before = _settings_env_vars(git.show_file(base_sha, f.path) if f.status != "A" else None)
            after = _settings_env_vars(git.show_file(head_sha, f.path) if f.status != "D" else None)
            for key in sorted(set(after) - set(before)):
                col.add(ConfigChangeKind.ENV_VAR_ADDED, key, Evidence(file=f.path, line=after[key], snippet="BaseSettings field"),
                        is_secret_hint=_secret_hint(key))
            for key in sorted(set(before) - set(after)):
                col.add(ConfigChangeKind.ENV_VAR_REMOVED, key, Evidence(file=f.path, line=before[key], snippet="BaseSettings field"))

        # ---- references in code / Dockerfiles --------------------------------
        patterns = _env_patterns_for(f.path)
        if not patterns:
            continue
        for ln in fd.added:
            for pat in patterns:
                for key in pat.findall(ln.text):
                    if key in BUILTIN_VARS:
                        continue
                    if not _grep_scoped(git, base_sha, key, comp_scope):
                        col.add(ConfigChangeKind.ENV_VAR_ADDED, key,
                                Evidence(file=f.path, line=ln.line, snippet=ln.text.strip()[:200]),
                                is_secret_hint=_secret_hint(key))
        for ln in fd.removed:
            for pat in patterns:
                for key in pat.findall(ln.text):
                    if key in BUILTIN_VARS:
                        continue
                    still_used = _grep_scoped(git, head_sha, key, comp_scope)
                    if not still_used:
                        col.add(ConfigChangeKind.ENV_VAR_REMOVED, key,
                                Evidence(file=f.path, line=ln.line, snippet=ln.text.strip()[:200]))

    # ---- enrich with manifest declarations -----------------------------------
    for item in col.items.values():
        if item.kind == ConfigChangeKind.ENV_VAR_ADDED and item.key:
            spec = declared.get(item.key)
            if spec is not None:
                item.description = item.description or spec.description
                item.is_secret_hint = item.is_secret_hint or spec.secret
                item.default_value = item.default_value or spec.default
            item.requires_value = not item.default_value
            if not item.description:
                item.description = "New environment variable referenced by the code"
        if item.kind == ConfigChangeKind.ENV_VAR_REMOVED and not item.description:
            item.description = "No longer referenced; can be removed from environments after rollout"

    # A var that is both "added" and "removed" (moved between files) is neither.
    added = {k for (kind, k) in col.items if kind == ConfigChangeKind.ENV_VAR_ADDED.value}
    removed = {k for (kind, k) in col.items if kind == ConfigChangeKind.ENV_VAR_REMOVED.value}
    for k in added & removed:
        col.items.pop((ConfigChangeKind.ENV_VAR_REMOVED.value, k), None)

    return ChangeReport(
        app=app,
        component=component.name,
        repo_url=repo_url,
        source_branch=source_branch,
        target_branch=target_branch,
        base_sha=base_sha,
        head_sha=head_sha,
        commits=commits,
        changed_files=files,
        config_changes=col.result(),
        requires_rebuild=requires_rebuild,
        requires_migration=requires_migration,
        warnings=sorted(set(warnings), key=warnings.index),
        summary=deterministic_summary(files, col.result(), requires_rebuild, requires_migration, len(commits)),
        analyzer_version=ANALYZER_VERSION,
    )


_GREP_EXCLUDES = (
    ":(exclude,glob)**/devops-agent.y*ml",
    ":(exclude,glob)**/.devops-agent.yaml",
    ":(exclude,glob)**/*.md",
    ":(exclude,glob)**/*.rst",
)


def _grep_scoped(git: _GuardedGit, ref: str, word: str, scope: str) -> bool:
    """Is ``word`` referenced in code/config at ``ref`` (ignoring manifests and docs)?"""
    r = git.run("grep", "-q", "-w", "-F", "-e", word, ref, "--", scope, *_GREP_EXCLUDES, check=False)
    return r.returncode == 0


def deterministic_summary(
    files: list[FileChange], changes: list[ConfigChange], rebuild: bool, migration: bool, n_commits: int
) -> str:
    added = [c.key for c in changes if c.kind == ConfigChangeKind.ENV_VAR_ADDED]
    removed = [c.key for c in changes if c.kind == ConfigChangeKind.ENV_VAR_REMOVED]
    cfg = [c.key for c in changes if c.kind == ConfigChangeKind.CONFIG_FILE_CHANGED]
    parts = [f"{n_commits} commit(s), {len(files)} file(s) changed."]
    if added:
        parts.append(f"New env vars: {', '.join(k for k in added if k)}.")
    if removed:
        parts.append(f"Env vars no longer used: {', '.join(k for k in removed if k)}.")
    if cfg:
        parts.append(f"Config files changed: {', '.join(k for k in cfg if k)}.")
    if rebuild:
        parts.append("Dependencies or build definition changed (rebuild images).")
    if migration:
        parts.append("New DB migration(s) must run before rollout.")
    if not (added or removed or cfg or rebuild or migration):
        parts.append("No configuration impact detected.")
    return " ".join(parts)
