from __future__ import annotations

from pathlib import Path

import pytest

from devops_common.gitguard import (
    DEFAULT_MERGE_IDENTITY,
    GitCommandError,
    GitPolicyError,
    MergeGit,
    ReadOnlyGit,
    WorkspaceGit,
)
from devops_common.testing import commit_and_push, git, init_remote


@pytest.mark.parametrize(
    "args",
    [
        ["commit", "-m", "x"],
        ["push", "origin", "main"],
        ["merge", "feature"],
        ["rebase", "main"],
        ["reset", "--hard"],
        ["checkout", "main"],
        ["add", "."],
        ["stash"],
        ["tag", "v1"],
        ["branch", "new-branch"],
        ["branch", "-D", "main"],
        ["config", "user.name", "evil"],
        ["diff", "--output=/tmp/x"],
        ["symbolic-ref", "HEAD", "refs/heads/other"],
        ["remote", "add", "x", "url"],
        ["clone", "-u", "sh", "url"],
        ["fetch", "--upload-pack=touch /tmp/pwned", "origin"],
    ],
)
def test_read_only_git_denies_writes(tmp_path: Path, args: list[str]) -> None:
    g = ReadOnlyGit(tmp_path)
    with pytest.raises(GitPolicyError):
        g.check(args)


def test_read_only_git_allows_inspection(backend_repo: tuple[Path, Path]) -> None:
    _, clone = backend_repo
    g = ReadOnlyGit(clone)
    assert g.is_repo()
    assert g.current_branch() == "main"
    assert g.remote_url() is not None
    g.fetch()
    assert len(g.rev_parse("HEAD")) == 40
    assert g.status_porcelain() == []
    g.check(["branch", "--list", "feat*"])
    g.check(["config", "--get", "remote.origin.url"])


def test_workspace_git_confined_to_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(GitPolicyError):
        WorkspaceGit(tmp_path / "elsewhere", ws)
    w = WorkspaceGit(ws / "repo", ws)
    with pytest.raises(GitPolicyError):
        w.check(["commit", "-m", "x"])
    with pytest.raises(GitPolicyError):
        w.check(["push", "origin", "main"])
    with pytest.raises(GitPolicyError):
        w.check(["checkout", "main"])  # only --detach
    w.check(["checkout", "--detach", "abc"])


def test_workspace_clone_and_checkout(backend_repo: tuple[Path, Path], tmp_path: Path) -> None:
    remote, _ = backend_repo
    ws = tmp_path / "ws"
    w = WorkspaceGit(ws / "backend", ws)
    w.clone(str(remote))
    w.checkout_detached("origin/main")
    assert (ws / "backend" / "app" / "main.py").is_file()


def test_merge_git_push_restrictions(tmp_path: Path) -> None:
    m = MergeGit(tmp_path)
    for bad in (
        ["push", "origin", "+HEAD:refs/heads/main"],
        ["push", "origin", ":refs/heads/main"],
        ["push", "--force", "origin", "HEAD:refs/heads/main"],
        ["push", "origin", "main"],
        ["commit", "-m", "sneaky"],
        ["add", "file.py"],
    ):
        with pytest.raises(GitPolicyError):
            m.check(bad)
    m.check(["push", "origin", "HEAD:refs/heads/develop"])


def test_merge_verified_clean_merge_and_push(tmp_path: Path) -> None:
    remote, dev = init_remote(tmp_path, "svc", {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    sha = commit_and_push(dev, {"a.py": "a = 2\n"}, "change a", "feature/a")
    # meanwhile main moves on (someone else merged)
    git(dev, "checkout", "-q", "main")
    commit_and_push(dev, {"b.py": "b = 2\n"}, "change b", "main")

    m = MergeGit(tmp_path / "server" / "svc")
    m.ensure_clone(str(remote))
    m.sync()
    assert m.commit_on_branch(sha, "feature/a")
    res = m.merge_verified(sha, "main", "Merge feature/a")
    assert res.ok and res.merge_sha
    m.push_merge("main")
    m.sync()
    assert m.rev_parse("origin/main") == res.merge_sha
    # merging again is a no-op
    again = m.merge_verified(sha, "main", "Merge feature/a")
    assert again.ok and again.already_merged


def test_merge_verified_reports_conflicts(tmp_path: Path) -> None:
    remote, dev = init_remote(tmp_path, "svc", {"a.py": "a = 1\n"})
    sha = commit_and_push(dev, {"a.py": "a = 'feature'\n"}, "feature edit", "feature/x")
    git(dev, "checkout", "-q", "main")
    commit_and_push(dev, {"a.py": "a = 'main'\n"}, "main edit", "main")
    m = MergeGit(tmp_path / "server" / "svc")
    m.ensure_clone(str(remote))
    m.sync()
    res = m.merge_verified(sha, "main", "Merge feature/x")
    assert not res.ok
    assert res.conflict_files == ["a.py"]
    # workspace restored, nothing pushed
    assert m.status_porcelain() == []
    m.sync()
    assert git(dev, "rev-parse", "origin/main") == m.rev_parse("origin/main")


def _feature_ready_to_merge(tmp_path: Path) -> tuple[Path, str]:
    remote, dev = init_remote(tmp_path, "svc", {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    sha = commit_and_push(dev, {"a.py": "a = 2\n"}, "change a", "feature/a")
    git(dev, "checkout", "-q", "main")
    commit_and_push(dev, {"b.py": "b = 2\n"}, "change b", "main")
    return remote, sha


def test_merge_commit_uses_explicit_identity_not_host_config(tmp_path: Path) -> None:
    # The workspace conftest hides ~/.gitconfig, so this also proves merges work on
    # hosts with no git user.name / user.email (e.g. CI runners).
    remote, sha = _feature_ready_to_merge(tmp_path)
    default = MergeGit(tmp_path / "server" / "default")
    default.ensure_clone(str(remote))
    default.sync()
    res = default.merge_verified(sha, "main", "Merge feature/a")
    assert res.ok
    name, email = DEFAULT_MERGE_IDENTITY
    assert default.out("log", "-1", "--format=%an <%ae>|%cn <%ce>", res.merge_sha) == f"{name} <{email}>|{name} <{email}>"

    custom = MergeGit(tmp_path / "server" / "custom", identity=("Release Bot", "release-bot@example.com"))
    custom.ensure_clone(str(remote))
    custom.sync()
    res = custom.merge_verified(sha, "main", "Merge feature/a")
    assert res.ok
    assert custom.out("log", "-1", "--format=%cn <%ce>", res.merge_sha) == "Release Bot <release-bot@example.com>"


def test_merge_failure_without_conflicts_raises_instead_of_reporting_conflict(tmp_path: Path) -> None:
    remote, sha = _feature_ready_to_merge(tmp_path)
    m = MergeGit(tmp_path / "server" / "svc", identity=("", ""))  # git refuses an empty ident
    m.ensure_clone(str(remote))
    m.sync()
    before = m.rev_parse("origin/main")
    with pytest.raises(GitCommandError, match="ident"):
        m.merge_verified(sha, "main", "Merge feature/a")
    # workspace restored, nothing pushed
    assert m.status_porcelain() == []
    assert m.rev_parse("HEAD") == before
    m.sync()
    assert m.rev_parse("origin/main") == before
