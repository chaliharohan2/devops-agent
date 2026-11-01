"""Capability-restricted git wrappers.

The fleet's core safety rules are enforced here, in code, not in prompts:

* :class:`ReadOnlyGit`   — used by local agents on the *developer's* repositories.
  Can inspect history and fetch remote-tracking refs. Can never commit, push,
  merge, rebase, reset, checkout, stash, tag or otherwise touch the working tree.
* :class:`WorkspaceGit`  — used on clones the agent owns (its cache directory).
  May clone/fetch/checkout a detached commit so it can build other components,
  but still never commits or pushes.
* :class:`MergeGit`      — used only by the server agent. May merge a pushed
  commit into a target branch (``--no-ff``, never resolving conflicts) and push
  that merge. It has no way to write files or create non-merge commits, so it
  cannot change code; :meth:`MergeGit.merge_verified` additionally proves that
  the merge only contains changes that came from the developer's branch.

Any attempt to run a subcommand outside an allow-list raises
:class:`GitPolicyError` *before* a process is spawned.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT = 120


class GitPolicyError(PermissionError):
    """Raised when an agent attempts a git operation outside its capabilities."""


class GitCommandError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str):
        self.args_ = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"git {' '.join(shlex.quote(a) for a in args)} failed ({returncode}): {stderr.strip()[:500]}")


# Flags that write files or execute arbitrary programs are never allowed.
_FORBIDDEN_FLAG_PREFIXES = ("--output", "--exec", "--upload-pack", "--receive-pack", "--ext-diff", "-c", "--config")

_READ_ONLY_SUBCOMMANDS: dict[str, set[str] | None] = {
    # None => any (non-forbidden) flags allowed
    "rev-parse": None,
    "merge-base": None,
    "log": None,
    "diff": None,
    "show": None,
    "ls-files": None,
    "ls-tree": None,
    "status": None,
    "grep": None,
    "cat-file": None,
    "rev-list": None,
    "for-each-ref": None,
    "describe": None,
    "ls-remote": None,
    "fetch": None,  # only updates remote-tracking refs; never the working tree
    "shortlog": None,
    "blame": None,
    "count-objects": None,
    "check-ignore": None,
    # restricted forms
    "remote": {"get-url", "-v", "show"},
    "branch": {"--list", "--show-current", "-r", "-a", "--remotes", "--all", "--contains", "--format", "-vv", "--no-color"},
    "config": {"--get", "--get-regexp", "--list", "-l", "--get-all"},
    "symbolic-ref": {"--short", "-q", "--quiet", "HEAD"},
    "bundle": {"create", "verify", "list-heads"},
    "archive": {"--format", "--prefix"},  # streamed to stdout only (no --output / --remote)
}

_WORKSPACE_EXTRA: dict[str, set[str] | None] = {
    "clone": None,
    "checkout": {"--detach", "--force", "-f", "-q", "--quiet"},
    "reset": {"--hard", "-q", "--quiet"},
    "clean": {"-fdx", "-fd", "-q", "-f", "-d", "-x"},
    "init": None,
    "remote": {"get-url", "-v", "show", "add", "set-url"},
    "pull": None,  # equivalent to fetch + ff in an agent-owned clone
}

_MERGE_EXTRA: dict[str, set[str] | None] = {
    "clone": None,
    "checkout": {"-B", "--detach", "-q", "--quiet", "--force", "-f"},
    "merge": {"--no-ff", "--no-edit", "--abort", "-m", "--no-verify", "--quiet", "-q", "--no-log"},
    "push": {"origin"},  # validated further in MergeGit.push
    "reset": {"--hard", "-q", "--quiet"},
    "clean": {"-fdx", "-fd", "-q", "-f", "-d", "-x"},
    "remote": {"get-url", "-v", "show", "set-url"},
    "init": None,
}


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",  # `status` must not refresh/write the index
            "GIT_ASKPASS": "",
            "LC_ALL": "C",
            # Only well-known transports; blocks ext:: and other command-executing remotes.
            "GIT_ALLOW_PROTOCOL": "file:git:http:https:ssh",
        }
    )
    return env


@dataclass
class GitResult:
    stdout: str
    stderr: str
    returncode: int


class _GuardedGit:
    capability = "read-only"
    allowed: dict[str, set[str] | None] = _READ_ONLY_SUBCOMMANDS

    def __init__(self, repo: Path | str):
        self.repo = Path(repo)

    def _env(self) -> dict[str, str]:
        return _git_env()

    # -- policy -------------------------------------------------------------
    def check(self, args: list[str]) -> None:
        if not args:
            raise GitPolicyError("empty git command")
        sub = args[0]
        if sub not in self.allowed:
            raise GitPolicyError(f"git {sub!r} is not permitted for a {self.capability} agent")
        for a in args[1:]:
            if any(a == p or a.startswith(p + "=") for p in _FORBIDDEN_FLAG_PREFIXES):
                raise GitPolicyError(f"git flag {a!r} is not permitted")
        if sub == "clone" and "-u" in args[1:]:
            raise GitPolicyError("git clone -u (upload-pack) is not permitted")
        restricted = self.allowed[sub]
        if restricted is None:
            return
        deny = GitPolicyError
        rest = args[1:]
        flags = [a.split("=", 1)[0] for a in rest if a.startswith("-")]
        positionals = [a for a in rest if not a.startswith("-")]
        for f in flags:
            if f not in restricted:
                raise deny(f"git {sub} {f!r} is not permitted for a {self.capability} agent")
        # Sub-commands whose positional arguments switch them into write modes.
        if sub in {"remote", "bundle"}:
            if not rest or rest[0] not in restricted:
                raise deny(f"git {sub} {rest[:1]} is not permitted for a {self.capability} agent")
        elif sub == "config":
            if not any(f in {"--get", "--get-regexp", "--list", "-l", "--get-all"} for f in flags):
                raise deny("git config may only be read")
        elif sub == "branch":
            if positionals and not ({"--list", "--contains"} & set(flags)):
                raise deny("git branch may only list branches")
        elif sub == "symbolic-ref":
            if len(positionals) > 1:
                raise deny("git symbolic-ref may only be read")
        elif sub == "checkout":
            if not ({"--detach", "-B"} & set(flags)):
                raise deny("checkout is only permitted as --detach (workspace) or -B (merge workspace)")

    # -- execution ----------------------------------------------------------
    def run(self, *args: str, check: bool = True, cwd: Path | None = None, timeout: int = GIT_TIMEOUT) -> GitResult:
        argv = list(args)
        self.check(argv)
        cmd = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *argv]
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
            env=self._env(),
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise GitCommandError(argv, proc.returncode, proc.stdout, proc.stderr)
        return GitResult(proc.stdout, proc.stderr, proc.returncode)

    def run_bytes(self, *args: str, timeout: int = GIT_TIMEOUT) -> bytes:
        argv = list(args)
        self.check(argv)
        cmd = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *argv]
        proc = subprocess.run(cmd, cwd=str(self.repo), capture_output=True, env=self._env(), timeout=timeout)
        if proc.returncode != 0:
            raise GitCommandError(argv, proc.returncode, "", proc.stderr.decode(errors="replace"))
        return proc.stdout

    def export_tree(self, ref: str, dest: Path, subpath: str = ".") -> Path:
        """Write the files of ``ref`` (optionally a sub-directory) to ``dest`` via ``git archive``."""
        import io
        import tarfile

        sub = subpath.strip("/").strip(".")
        data = self.run_bytes("archive", "--format=tar", ref, *([sub] if sub else []), timeout=600)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")
        return dest / sub if sub else dest

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    # -- convenience readers -------------------------------------------------
    def is_repo(self) -> bool:
        try:
            return self.run("rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"
        except (FileNotFoundError, NotADirectoryError):
            return False

    def toplevel(self) -> Path:
        return Path(self.out("rev-parse", "--show-toplevel"))

    def rev_parse(self, ref: str) -> str:
        return self.out("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")

    def try_rev_parse(self, ref: str) -> str | None:
        r = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None

    def current_branch(self) -> str | None:
        b = self.run("branch", "--show-current", check=False).stdout.strip()
        return b or None

    def remote_url(self, remote: str = "origin") -> str | None:
        r = self.run("remote", "get-url", remote, check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def merge_base(self, a: str, b: str) -> str:
        return self.out("merge-base", a, b)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.run("merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0

    def status_porcelain(self) -> list[str]:
        return [ln for ln in self.run("status", "--porcelain=v1", "--untracked-files=normal").stdout.splitlines() if ln]

    def fetch(self, remote: str = "origin", *refspecs: str) -> None:
        self.run("fetch", "--quiet", remote, *refspecs, timeout=300)

    def show_file(self, ref: str, path: str) -> str | None:
        r = self.run("show", f"{ref}:{path}", check=False)
        return r.stdout if r.returncode == 0 else None

    def grep_word(self, ref: str, word: str) -> bool:
        r = self.run("grep", "-q", "-w", "-F", "-e", word, ref, "--", ".", check=False)
        return r.returncode == 0

    def changed_paths(self, a: str, b: str) -> set[str]:
        out = self.run("diff", "--name-only", "--no-renames", a, b).stdout
        return {ln for ln in out.splitlines() if ln}


class ReadOnlyGit(_GuardedGit):
    """Git access for local agents on developer-owned repositories (inspect only)."""

    capability = "read-only"
    allowed = _READ_ONLY_SUBCOMMANDS


class WorkspaceGit(_GuardedGit):
    """Git access on clones owned by the agent (e.g. ~/.devops-agent/workspaces).

    The repository must live under ``workspace_root`` so the extra capabilities
    (checkout/reset/clean) can never be pointed at a developer's own checkout.
    """

    capability = "workspace"
    allowed = {**_READ_ONLY_SUBCOMMANDS, **_WORKSPACE_EXTRA}

    def __init__(self, repo: Path | str, workspace_root: Path | str):
        super().__init__(repo)
        root = Path(workspace_root).resolve()
        target = Path(repo).resolve()
        if root != target and root not in target.parents:
            raise GitPolicyError(f"{target} is outside the agent workspace {root}")
        self.workspace_root = root

    def clone(self, url: str, *, depth: int | None = None) -> None:
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--quiet", "--no-checkout"]
        if depth:
            args += ["--depth", str(depth), "--no-single-branch"]
        args += [url, str(self.repo)]
        self.run(*args, cwd=self.repo.parent, timeout=600)

    def checkout_detached(self, ref: str) -> None:
        self.run("checkout", "--quiet", "--force", "--detach", ref)


@dataclass
class MergeResult:
    ok: bool
    merge_sha: str | None = None
    target_before: str | None = None
    conflict_files: list[str] | None = None
    message: str = ""
    already_merged: bool = False


DEFAULT_MERGE_IDENTITY = ("DevOps Server Agent", "devops-agent@localhost")


class MergeGit(_GuardedGit):
    """Server agent's git capability: merge pushed commits, never author code.

    Merge commits are made under an explicit ``identity`` (name, email) passed via
    ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*``, so merges never depend on whatever
    ``user.name`` / ``user.email`` the host happens to have configured.
    """

    capability = "merge-only"
    allowed = {**_READ_ONLY_SUBCOMMANDS, **_MERGE_EXTRA}

    def __init__(self, repo: Path | str, identity: tuple[str, str] = DEFAULT_MERGE_IDENTITY):
        super().__init__(repo)
        self.identity = identity

    def _env(self) -> dict[str, str]:
        env = super()._env()
        name, email = self.identity
        env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                    "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
        return env

    def check(self, args: list[str]) -> None:
        super().check(args)
        if args[0] == "push":
            # Only "push origin HEAD:refs/heads/<branch>" — never force, never delete.
            if len(args) != 3 or args[1] != "origin" or not args[2].startswith("HEAD:refs/heads/"):
                raise GitPolicyError("server agent may only push a merge result: push origin HEAD:refs/heads/<branch>")
            if args[2].startswith("+") or ":" not in args[2] or args[2].endswith(":"):
                raise GitPolicyError("force pushes and ref deletions are not permitted")

    def ensure_clone(self, url: str) -> None:
        if (self.repo / ".git").is_dir():
            current = self.remote_url()
            if current != url:
                self.run("remote", "set-url", "origin", url)
            return
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        self.run("clone", "--quiet", url, str(self.repo), cwd=self.repo.parent, timeout=900)

    def sync(self) -> None:
        self.fetch("origin", "+refs/heads/*:refs/remotes/origin/*")

    def commit_on_branch(self, sha: str, branch: str) -> bool:
        ref = f"refs/remotes/origin/{branch}"
        if self.try_rev_parse(ref) is None:
            return False
        return self.is_ancestor(sha, ref)

    def merge_verified(self, source_sha: str, target_branch: str, message: str) -> MergeResult:
        """Merge ``source_sha`` into ``origin/target_branch`` locally and verify it.

        Verification proves the server agent introduced no code of its own: the
        set of paths changed by the merge (relative to the old target tip) must be
        a subset of the paths the developer's branch changed (relative to the
        merge base). Conflicts are never resolved automatically. A merge that fails
        for any other reason (e.g. git cannot create the commit) raises
        :class:`GitCommandError` instead of being reported as a conflict.
        """
        target_ref = f"refs/remotes/origin/{target_branch}"
        target_before = self.rev_parse(target_ref)
        if self.is_ancestor(source_sha, target_before):
            return MergeResult(ok=True, merge_sha=target_before, target_before=target_before,
                               message="commit already contained in target", already_merged=True)
        self.run("checkout", "--quiet", "--force", "-B", "_devops_agent_merge", target_ref)
        self.run("reset", "--hard", "--quiet", target_ref)
        self.run("clean", "-fdx", "-q")
        if self.status_porcelain():
            raise GitPolicyError("merge workspace is not clean; refusing to merge")
        merge_args = ["merge", "--no-ff", "--no-edit", "-m", message, source_sha]
        r = self.run(*merge_args, check=False)
        if r.returncode != 0:
            conflicts = [
                ln.split("\t", 1)[-1] if "\t" in ln else ln[3:]
                for ln in self.run("status", "--porcelain=v1").stdout.splitlines()
                if ln[:2] in {"UU", "AA", "DU", "UD", "AU", "UA", "DD"}
            ]
            self.run("merge", "--abort", check=False)
            self.run("reset", "--hard", "--quiet", target_ref)
            if not conflicts:
                raise GitCommandError(merge_args, r.returncode, r.stdout, r.stderr)
            return MergeResult(ok=False, target_before=target_before, conflict_files=conflicts,
                               message=(r.stdout + r.stderr).strip()[:2000])
        merge_sha = self.rev_parse("HEAD")
        base = self.merge_base(target_before, source_sha)
        dev_paths = self.changed_paths(base, source_sha)
        merge_paths = self.changed_paths(target_before, merge_sha)
        extra = merge_paths - dev_paths
        if extra:
            self.run("reset", "--hard", "--quiet", target_ref)
            raise GitPolicyError(f"merge result touches paths not changed by the developer: {sorted(extra)[:20]}")
        parents = self.out("rev-list", "--parents", "-n", "1", merge_sha).split()[1:]
        if parents != [target_before, source_sha]:
            self.run("reset", "--hard", "--quiet", target_ref)
            raise GitPolicyError("unexpected merge parents; refusing to push")
        return MergeResult(ok=True, merge_sha=merge_sha, target_before=target_before)

    def push_merge(self, target_branch: str) -> None:
        self.run("push", "origin", f"HEAD:refs/heads/{target_branch}", timeout=300)
