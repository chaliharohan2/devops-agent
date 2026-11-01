from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest
import yaml

from devops_common.llm import LLMClient
from devops_common.protocol import ConfigValue
from devops_common.testing import commit_and_push, git, write_files
from devops_local import changes, hardware
from devops_local.brain import Brain
from devops_local.repos import normalize_url
from devops_local.service import AgentError


def ok_runner(calls: list):
    def run(cmd, cwd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    return run


# --------------------------------------------------------------------------- unit


def _hw(total: float, flags=("avx2",)) -> hardware.Hardware:
    return hardware.Hardware(os="Linux", arch="x86_64", cpu_cores=8, cpu_threads=16, cpu_flags=list(flags),
                             total_ram_gb=total, available_ram_gb=total - 4, free_disk_gb=400, gpu=None)


def test_model_recommendation_for_team_default_laptop() -> None:
    rec, notes = hardware.recommend(_hw(20))
    assert rec.key == "ornith-1.5-9b"
    assert rec.ram_gb <= 20 - 9
    small, _ = hardware.recommend(_hw(12))
    assert small.key == "gemma-4-e4b"
    _, notes = hardware.recommend(_hw(20, flags=()))
    assert any("AVX2" in n for n in notes)


def test_normalize_url() -> None:
    assert normalize_url("git@github.com:Org/Repo.git") == "github.com/org/repo"
    assert normalize_url("https://github.com/org/repo") == "github.com/org/repo"
    assert normalize_url("https://user@github.com/org/repo.git/") == "github.com/org/repo"


# ---------------------------------------------------------------------- deploy flow


def test_setup_autolinks_repos(world) -> None:
    agent = world.agent_for("alice")
    linked = agent.auto_link()
    assert set(linked) == {"shop/backend", "shop/frontend"}


def test_preflight_blocks_unpushed_branch_and_warns(world) -> None:
    agent = world.agent_for("alice")
    agent.auto_link()
    repo = agent.cfg.repo_for("shop", "backend")
    git(repo, "checkout", "-q", "-b", "feature/local-only")
    commit_and_push(repo, {"app/x.py": "X = 1\n"}, "local", "feature/local-only", push=False)
    pf = changes.preflight(repo, None, "main")
    assert not pf.ok and "git push -u origin feature/local-only" in pf.problems[0]
    git(repo, "push", "-q", "-u", "origin", "feature/local-only")
    commit_and_push(repo, {"app/y.py": "Y = 1\n"}, "unpushed", "feature/local-only", push=False)
    write_files(repo, {"app/z.py": "Z = 1\n"})
    pf = changes.preflight(repo, None, "main")
    assert pf.ok
    assert pf.unpushed_commits == 1
    assert any("not pushed" in w for w in pf.warnings) and any("uncommitted" in w for w in pf.warnings)


def test_local_agent_never_pushes(world) -> None:
    agent = world.agent_for("alice")
    agent.auto_link()
    repo = agent.cfg.repo_for("shop", "backend")
    git(repo, "checkout", "-q", "-b", "feature/p")
    commit_and_push(repo, {"app/p.py": "P = 1\n"}, "p", "feature/p", push=False)
    with pytest.raises(AgentError, match="not on the remote"):
        agent.deploy("shop", "backend", "dev")
    remote_branches = git(world.backend_remote, "branch", "--list")
    assert "feature/p" not in remote_branches


def test_deploy_end_to_end_through_server(world) -> None:
    agent = world.agent_for("alice")
    agent.auto_link()
    repo = agent.cfg.repo_for("shop", "backend")
    commit_and_push(repo, {"app/pay.py": 'import os\nKEY = os.environ["PAYMENT_API_KEY"]\n'}, "pay", "feature/pay")
    res = agent.analyze("shop", "backend", "dev")
    assert res["preflight"]["problems"] == []
    assert [v["key"] for v in res["values_needed"]] == ["PAYMENT_API_KEY"]
    assert res["values_needed"][0]["secret"] is True
    cr = agent.deploy("shop", "backend", "dev", [ConfigValue(key="PAYMENT_API_KEY", value="sk_1", is_secret=True)])
    final = agent.deploy_status(cr.id)[0]
    assert final.status.value == "deployed", final
    git(repo, "fetch", "-q")
    assert git(repo, "rev-parse", "origin/main") == final.merge_sha
    events = agent.poll_inbox()
    assert events and events[-1].payload["status"] == "deployed"


def test_resolve_from_cwd(world, monkeypatch) -> None:
    agent = world.agent_for("alice")
    agent.auto_link()
    monkeypatch.chdir(agent.cfg.repo_for("shop", "frontend") / "src")
    assert agent.resolve(None, None) == ("shop", "frontend")


# ------------------------------------------------------------------- test envs


def test_test_environment_renders_local_and_server_code(world) -> None:
    calls: list = []
    agent = world.agent_for("alice", runner=ok_runner(calls))
    agent.auto_link()
    # alice has an uncommitted backend change: it must be part of the test env (mounted)
    backend = agent.cfg.repo_for("shop", "backend")
    write_files(backend, {"app/wip.py": "WIP = True\n"})
    agent.envs.wait_healthy = lambda st, t, log=None: {k: "healthy" for k in st.urls}
    st = agent.env_up("shop", "dev", ["backend"])
    assert st.status == "running"
    assert st.sources["backend"]["origin"] == "local" and st.sources["backend"]["path"] == str(backend.resolve())
    assert st.sources["frontend"]["origin"] == "clone"
    fe_src = Path(st.sources["frontend"]["path"])
    assert (fe_src / "package.json").is_file() and not (fe_src / ".git").exists()
    doc = yaml.safe_load((agent.cfg.envs_dir / st.name / "docker-compose.yml").read_text())
    be = doc["services"]["backend"]
    assert be["volumes"][0] == f"{backend.resolve()}:/app"
    assert be["command"].startswith("uvicorn") and "--reload" in be["command"]
    assert be["environment"]["DATABASE_URL"].endswith("@postgres:5432/app")
    assert be["environment"]["CORS_ORIGINS"] == st.urls["frontend"]
    assert doc["services"]["frontend"]["environment"]["VITE_API_URL"] == st.urls["backend"]
    assert "backend-migrate" in doc["services"] and "postgres" in doc["services"]
    assert ["up", "-d", "--build", "--remove-orphans"] == calls[0][-4:]
    assert st.urls["frontend"] in st.instructions and "live reload" in st.instructions
    # rebuilding keeps the same ports
    again = agent.env_up("shop", "dev", ["backend"])
    assert again.ports == st.ports
    other = agent.env_up("shop", "dev", ["frontend"])
    assert set(other.ports.values()).isdisjoint(st.ports.values())
    agent.env_down(st.name)
    assert agent.envs.get(st.name).status == "stopped"


def test_test_environment_unknown_component(world) -> None:
    agent = world.agent_for("alice", runner=ok_runner([]))
    with pytest.raises(AgentError, match="unknown component"):
        agent.env_up("shop", "dev", ["payments"])


def test_test_environment_requires_link(world) -> None:
    agent = world.agent_for("alice", runner=ok_runner([]))
    with pytest.raises(AgentError, match="not linked"):
        agent.env_up("shop", "dev", ["backend"])


# ------------------------------------------------------------------------ peers


def test_peer_working_tree_share_includes_uncommitted_work(world) -> None:
    alice = world.agent_for("alice", runner=ok_runner([]))
    bob = world.agent_for("bob", runner=ok_runner([]))
    alice.auto_link()
    bob.auto_link()
    bob_fe = bob.cfg.repo_for("shop", "frontend")
    write_files(bob_fe, {"src/NewFeature.tsx": "export const x = 1\n", "node_modules/big.js": "ignored?"})
    (bob_fe / ".gitignore").write_text("node_modules/\n")
    pr = alice.peer_request("shop", "frontend", "bob", message="want to test with your UI")
    assert pr.share.value == "working_tree"
    bob.poll_inbox()
    assert bob.notifications[-1]["kind"] == "peer_request"
    shared = bob.peer_respond(pr.id, True)
    assert shared.status.value == "uploaded" and shared.head_sha.endswith("+dirty")
    alice.envs.wait_healthy = lambda st, t, log=None: {k: "healthy" for k in st.urls}
    st = alice.env_up("shop", "dev", ["backend"], peer_requests={"frontend": pr.id})
    src = Path(st.sources["frontend"]["path"])
    assert st.sources["frontend"]["origin"] == "peer"
    assert (src / "src" / "NewFeature.tsx").is_file()
    assert not (src / "node_modules").exists()


def test_peer_branch_share_via_bundle(world) -> None:
    alice = world.agent_for("alice")
    bob = world.agent_for("bob")
    bob.auto_link()
    bob_be = bob.cfg.repo_for("shop", "backend")
    git(bob_be, "checkout", "-q", "-b", "feature/bob")
    sha = commit_and_push(bob_be, {"app/bob.py": "BOB = 1\n"}, "bob's work", "feature/bob", push=False)
    pr = alice.peer_request("shop", "backend", "bob", branch="feature/bob")
    shared = bob.peer_respond(pr.id, True)
    assert shared.head_sha == sha
    src = alice.peer_fetch(pr.id)
    assert (src / "app" / "bob.py").read_text() == "BOB = 1\n"
    # denying works too
    pr2 = alice.peer_request("shop", "backend", "bob", branch="feature/bob")
    assert bob.peer_respond(pr2.id, False).status.value == "denied"


def test_snapshot_is_valid_tarball(world, tmp_path) -> None:
    from devops_local.peers import snapshot_working_tree

    bob = world.agent_for("bob")
    bob.auto_link()
    data, head = snapshot_working_tree(bob.cfg.repo_for("shop", "backend"))
    p = tmp_path / "s.tgz"
    p.write_bytes(data)
    with tarfile.open(p) as t:
        assert "app/main.py" in t.getnames()


# ------------------------------------------------------------------------ brain


def test_brain_rules_fallback_deploy_needs_confirmation(world, monkeypatch) -> None:
    agent = world.agent_for("alice")
    agent.auto_link()
    repo = agent.cfg.repo_for("shop", "backend")
    commit_and_push(repo, {"app/u.py": "U = 1\n"}, "u", "feature/u")
    monkeypatch.chdir(repo)
    brain = Brain(agent)
    first = brain.chat("please deploy my changes to dev")
    assert "(yes/no)" in first["reply"]
    assert agent.deploy_status() == []  # nothing sent before confirmation
    second = brain.chat("yes", first["session_id"])
    assert "change_request" in second["reply"]
    assert agent.deploy_status()[0].status.value == "deployed"


def test_brain_llm_tool_calling(world, monkeypatch) -> None:
    agent = world.agent_for("alice")
    agent.cfg.llm.enabled = True
    replies = [
        {"content": "", "tool_calls": [{"id": "1", "function": {"name": "deploy_status", "arguments": "{}"}}]},
        {"content": "You have no deploy requests yet."},
    ]
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": replies[len(seen) - 1]}]})

    from devops_local import model_manager

    monkeypatch.setattr(model_manager, "client", lambda cfg: LLMClient(
        "http://ollama/v1", "devops-agent-slm", transport=httpx.MockTransport(handler)))
    res = Brain(agent).chat("what's the status of my deploys?")
    assert res["source"] == "llm" and res["steps"][0]["tool"] == "deploy_status"
    assert res["reply"] == "You have no deploy requests yet."
    assert any(t["function"]["name"] == "setup_test_env" for t in seen[0]["tools"])


def test_brain_llm_mutating_tool_waits_for_yes(world, monkeypatch) -> None:
    agent = world.agent_for("alice")
    agent.cfg.llm.enabled = True
    call = {"id": "1", "function": {"name": "request_peer_code", "arguments": '{"from_user": "bob"}'}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": [call]}}]})

    from devops_local import model_manager

    monkeypatch.setattr(model_manager, "client", lambda cfg: LLMClient(
        "http://ollama/v1", "m", transport=httpx.MockTransport(handler)))
    res = Brain(agent).chat("get bob's code")
    assert res["pending"]["tool"] == "request_peer_code"
    assert agent.server.peers() == []  # proposed, not executed


# ------------------------------------------------------------------------ daemon


def test_daemon_csrf_guard_and_status(world) -> None:
    from fastapi.testclient import TestClient

    from devops_local.daemon import create_app

    agent = world.agent_for("alice")
    c = TestClient(create_app(agent, background=False))
    assert c.get("/api/status").json()["username"] == "alice"
    assert c.post("/api/repos/auto-link").status_code == 403
    assert c.post("/api/repos/auto-link", headers={"X-DevOps-Agent": "1", "Origin": "https://evil.example"}).status_code == 403
    r = c.post("/api/repos/auto-link", headers={"X-DevOps-Agent": "1"})
    assert r.status_code == 200 and "shop/backend" in r.json()
    assert c.post("/api/changes/analyze", json={"app": "shop", "component": "nope", "environment": "dev"},
                  headers={"X-DevOps-Agent": "1"}).status_code == 400


def test_already_merged_branch_is_nothing_to_deploy(world) -> None:
    agent = world.agent_for("alice")
    agent.auto_link()
    repo = agent.cfg.repo_for("shop", "backend")
    git(repo, "checkout", "-q", "-b", "feature/empty")
    git(repo, "push", "-q", "-u", "origin", "feature/empty")
    res = agent.analyze("shop", "backend", "dev")
    assert any("Nothing to deploy" in p for p in res["preflight"]["problems"])
    with pytest.raises(AgentError, match="Nothing to deploy"):
        agent.deploy("shop", "backend", "dev")


def test_cli_prints_untrusted_text_literally(monkeypatch) -> None:
    """LLM/server text must not be parsed as Rich markup ("[/etc/app]" used to raise MarkupError)."""
    import io

    from rich.console import Console

    from devops_local import cli

    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=200, color_system=None))
    cli._print_analysis({
        "app": "shop", "component": "backend", "target_branch": "main",
        "preflight": {"branch": "feature/x", "problems": ["[/etc/app] is dirty"], "warnings": ["[bold]w[/bold]"]},
        "report": {"summary": "Rollback: [/etc/app] config is reverted", "config_changes": [],
                   "warnings": ["[backend 2/5] RUN pip install"]},
    })
    text = out.getvalue()
    for literal in ("[/etc/app] is dirty", "[bold]w[/bold]", "Rollback: [/etc/app] config", "[backend 2/5] RUN"):
        assert literal in text
