"""End-to-end tests of the server agent pipeline using real git remotes."""

from __future__ import annotations

import json

import httpx

from devops_common.analysis import analyze_branch
from devops_common.gitguard import ReadOnlyGit
from devops_common.manifest import load_manifest
from devops_common.protocol import ConfigChangeKind
from devops_common.testing import commit_and_push, git

FEATURE = {
    "app/payments.py": 'import os\n\nKEY = os.environ["PAYMENT_API_KEY"]\nURL = os.getenv("PAYMENT_URL")\n',
    "alembic/versions/002_payments.py": "revision = '002'\n",
}


def local_report(env, branch: str, target: str = "main"):
    """What the developer's local agent would compute (read-only git on their clone)."""
    g = ReadOnlyGit(env.dev_clone)
    g.fetch()
    return analyze_branch(
        g, app="shop", component=load_manifest(env.dev_clone).get("backend"), base_ref=f"origin/{target}",
        head_ref=f"origin/{branch}", source_branch=branch, target_branch=target, repo_url=str(env.remote),
    )


def deploy_request(env, branch: str, environment: str, values=None, report=None):
    report = report or local_report(env, branch)
    return {
        "app": "shop", "component": "backend", "environment": environment, "source_branch": branch,
        "head_sha": report.head_sha, "report": report.model_dump(mode="json"), "config_values": values or [],
    }


PAYMENT_VALUES = [
    {"key": "PAYMENT_API_KEY", "value": "sk_test_123", "is_secret": True, "component": "backend"},
    {"key": "PAYMENT_URL", "value": "https://pay.example.com", "component": "backend"},
]


def test_auth_and_roles(shop) -> None:
    c = shop.client
    assert c.get("/api/v1/apps").status_code == 401
    dev = shop.user("alice")
    assert c.get("/api/v1/apps", headers=dev).status_code == 200
    assert c.post("/api/v1/apps", json={"name": "x"}, headers=dev).status_code == 403
    assert c.get("/api/v1/health").json()["ok"] is True


def test_full_pipeline_merges_configures_and_deploys(shop) -> None:
    c = shop.client
    dev = shop.user("alice")
    reg = c.post("/api/v1/agents/register", json={"hostname": "alice-laptop", "version": "0.1.0", "model": "ornith"},
                 headers=dev).json()
    agent = shop.headers(reg["agent_token"])
    commit_and_push(shop.dev_clone, FEATURE, "payments", "feature/payments")

    r = c.post("/api/v1/change-requests", json=deploy_request(shop, "feature/payments", "dev", PAYMENT_VALUES),
               headers=agent)
    assert r.status_code == 202, r.text
    cr = c.get(f"/api/v1/change-requests/{r.json()['id']}", headers=agent).json()
    assert cr["status"] == "deployed", cr
    assert cr["risk_level"] == "medium"  # new env vars + migration in a non-prod env
    assert cr["review"]["source"] == "deterministic"  # LLM disabled in tests
    assert cr["discrepancies"] == []
    statuses = [t["status"] for t in cr["timeline"]]
    assert statuses[:4] == ["received", "analyzing", "approved", "merging"] and statuses[-1] == "deployed"

    # merge landed on the remote with the developer's commit as second parent
    git(shop.dev_clone, "fetch", "-q")
    main = git(shop.dev_clone, "rev-parse", "origin/main")
    assert main == cr["merge_sha"]
    parents = git(shop.dev_clone, "rev-list", "--parents", "-n", "1", main).split()[1:]
    assert parents[1] == cr["head_sha"]

    # config store updated, secret masked everywhere
    cfg = {e["key"]: e for e in c.get("/api/v1/apps/shop/environments/dev/configs", headers=dev).json()}
    assert cfg["PAYMENT_URL"]["value"] == "https://pay.example.com"
    assert cfg["PAYMENT_API_KEY"]["is_secret"] and cfg["PAYMENT_API_KEY"]["value"] != "sk_test_123"
    dep = c.get(f"/api/v1/deployments/{cr['deployment_id']}", headers=dev).json()
    assert dep["status"] == "succeeded" and dep["target"] == "dry_run"
    be_env = dep["plan"]["compose"]["services"]["backend"]["environment"]
    assert be_env["PAYMENT_URL"] == "https://pay.example.com"
    assert be_env["PAYMENT_API_KEY"] != "sk_test_123"
    assert "sk_test_123" not in json.dumps(dep)
    assert "backend-migrate" in dep["plan"]["compose"]["services"]
    assert dep["component_shas"]["backend"] == cr["merge_sha"]

    # audit + inbox
    actions = [e["action"] for e in c.get("/api/v1/audit", headers=dev).json()]
    for a in ("change_request.create", "change_request.analyzed", "git.merge", "config.update", "deployment.succeeded"):
        assert a in actions
    inbox = c.get("/api/v1/agents/inbox", headers=agent).json()
    assert inbox[-1]["payload"]["status"] == "deployed"


def test_missing_config_waits_then_maintainer_unblocks(shop) -> None:
    c = shop.client
    dev = shop.user("alice")
    lead = shop.user("lead", "maintainer")
    commit_and_push(shop.dev_clone, FEATURE, "payments", "feature/payments")
    cr = c.post("/api/v1/change-requests", json=deploy_request(shop, "feature/payments", "dev"), headers=dev).json()
    cr = c.get(f"/api/v1/change-requests/{cr['id']}", headers=dev).json()
    assert cr["status"] == "awaiting_approval"
    assert set(cr["review"]["missing_config"]) == {"PAYMENT_API_KEY", "PAYMENT_URL"}
    # approving without config is refused...
    assert c.post(f"/api/v1/change-requests/{cr['id']}/approve", json={}, headers=lead).status_code == 409
    # ...developers cannot approve or edit configs
    assert c.post(f"/api/v1/change-requests/{cr['id']}/approve", json={}, headers=dev).status_code == 403
    assert c.put("/api/v1/apps/shop/environments/dev/configs", json={"values": PAYMENT_VALUES}, headers=dev).status_code == 403
    r = c.put("/api/v1/apps/shop/environments/dev/configs", json={"values": PAYMENT_VALUES, "message": "payments"},
              headers=lead)
    assert r.json()["config_version"] == 1
    cr = c.post(f"/api/v1/change-requests/{cr['id']}/approve", json={}, headers=lead).json()
    assert c.get(f"/api/v1/change-requests/{cr['id']}", headers=dev).json()["status"] == "deployed"
    hist = c.get("/api/v1/apps/shop/environments/dev/configs/history", headers=dev).json()
    assert hist[0]["version"] == 1 and hist[0]["actor"] == "lead"


def test_prod_requires_approval_and_no_self_approval(shop) -> None:
    c = shop.client
    lead = shop.user("lead", "maintainer")
    other = shop.user("other", "maintainer")
    commit_and_push(shop.dev_clone, {"app/util.py": "X = 1\n"}, "util", "feature/util")
    cr = c.post("/api/v1/change-requests", json=deploy_request(shop, "feature/util", "prod"), headers=lead).json()
    cr = c.get(f"/api/v1/change-requests/{cr['id']}", headers=lead).json()
    assert cr["status"] == "awaiting_approval"
    assert c.post(f"/api/v1/change-requests/{cr['id']}/approve", json={}, headers=lead).status_code == 403
    assert c.post(f"/api/v1/change-requests/{cr['id']}/approve", json={}, headers=other).status_code == 200
    assert c.get(f"/api/v1/change-requests/{cr['id']}", headers=lead).json()["status"] == "deployed"


def test_conflict_is_reported_never_resolved(shop) -> None:
    c = shop.client
    dev = shop.user("alice")
    commit_and_push(shop.dev_clone, {"app/main.py": "# feature version\n"}, "feature edit", "feature/conflict")
    report = local_report(shop, "feature/conflict")
    git(shop.dev_clone, "checkout", "-q", "main")
    commit_and_push(shop.dev_clone, {"app/main.py": "# main version\n"}, "main edit", "main")
    before = git(shop.dev_clone, "rev-parse", "origin/main")
    cr = c.post("/api/v1/change-requests", json=deploy_request(shop, "feature/conflict", "dev", report=report),
                headers=dev).json()
    cr = c.get(f"/api/v1/change-requests/{cr['id']}", headers=dev).json()
    assert cr["status"] == "conflict"
    assert "app/main.py" in cr["error"]
    git(shop.dev_clone, "fetch", "-q")
    assert git(shop.dev_clone, "rev-parse", "origin/main") == before


def test_unpushed_commit_is_rejected(shop) -> None:
    c = shop.client
    dev = shop.user("alice")
    commit_and_push(shop.dev_clone, {"app/a.py": "A = 1\n"}, "pushed", "feature/x")
    report = local_report(shop, "feature/x")
    unpushed = commit_and_push(shop.dev_clone, {"app/b.py": "B = 1\n"}, "not pushed", "feature/x", push=False)
    body = deploy_request(shop, "feature/x", "dev", report=report)
    body["head_sha"] = unpushed
    body["report"]["head_sha"] = unpushed
    cr = c.post("/api/v1/change-requests", json=body, headers=dev).json()
    cr = c.get(f"/api/v1/change-requests/{cr['id']}", headers=dev).json()
    assert cr["status"] == "failed" and "Push your branch" in cr["error"]


def test_discrepancy_between_local_and_server_needs_human(shop) -> None:
    c = shop.client
    dev = shop.user("alice")
    commit_and_push(shop.dev_clone, FEATURE, "payments", "feature/payments")
    report = local_report(shop, "feature/payments")
    # a buggy/tampered local agent "forgets" a secret it found
    report.config_changes = [c_ for c_ in report.config_changes if c_.key != "PAYMENT_API_KEY"]
    cr = c.post("/api/v1/change-requests", json=deploy_request(shop, "feature/payments", "dev", PAYMENT_VALUES, report),
                headers=dev).json()
    cr = c.get(f"/api/v1/change-requests/{cr['id']}", headers=dev).json()
    assert cr["status"] == "awaiting_approval"
    assert cr["risk_level"] == "high"
    assert any("PAYMENT_API_KEY" in d for d in cr["discrepancies"])


def test_environment_spec_never_contains_secrets(shop) -> None:
    c = shop.client
    lead = shop.user("lead", "maintainer")
    c.put("/api/v1/apps/shop/environments/dev/configs", headers=lead, json={"values": [
        {"key": "API_TOKEN", "value": "super-secret", "is_secret": True},
        {"key": "LOG_LEVEL", "value": "debug"},
        {"key": "LOG_LEVEL", "value": "info", "component": "backend"},
    ]})
    spec = c.get("/api/v1/apps/shop/environments/dev/spec", headers=lead).json()
    be = spec["components"][0]
    assert be["env"] == {"LOG_LEVEL": "info"}  # component scope overrides app scope
    assert be["secret_keys"] == ["API_TOKEN"]
    assert "super-secret" not in json.dumps(spec)
    assert be["manifest"]["framework"] == "fastapi" and be["sha"]
    assert spec["services"][0]["name"] == "postgres"


def test_invalid_config_key_rejected(shop) -> None:
    lead = shop.user("lead", "maintainer")
    r = shop.client.put("/api/v1/apps/shop/environments/dev/configs", headers=lead,
                        json={"values": [{"key": "lower-case", "value": "x"}]})
    assert r.status_code == 422


def test_import_components_from_manifest(server) -> None:
    c = server.client
    assert c.post("/api/v1/apps", json={"name": "shop"}, headers=server.admin).status_code == 201
    r = c.post("/api/v1/apps/shop/components/import", json={"repo_url": str(server.remote)}, headers=server.admin)
    assert r.status_code == 201, r.text
    assert r.json()[0]["name"] == "backend" and r.json()[0]["manifest"]["port"] == 8000


def test_llm_review_is_advisory_and_cannot_lower_risk(shop, monkeypatch) -> None:
    from devops_common.llm import LLMClient
    from devops_server.services import llm_service

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"risk_level": "low", "summary": "LLM says fine", "concerns": [],
                              "missing_config": [], "deployment_steps": ["deploy"]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    fake = LLMClient("http://fake-vllm/v1", "fake", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm_service, "get_client", lambda: fake)
    c = shop.client
    dev = shop.user("alice")
    commit_and_push(shop.dev_clone, FEATURE, "payments", "feature/payments")
    report = local_report(shop, "feature/payments")
    report.config_changes = [x for x in report.config_changes if x.kind != ConfigChangeKind.MIGRATION_ADDED]
    cr = c.post("/api/v1/change-requests", json=deploy_request(shop, "feature/payments", "dev", PAYMENT_VALUES, report),
                headers=dev).json()
    cr = c.get(f"/api/v1/change-requests/{cr['id']}", headers=dev).json()
    assert cr["review"]["source"] == "llm" and cr["review"]["summary"] == "LLM says fine"
    assert cr["risk_level"] == "high"  # discrepancy floor wins over the model's "low"
    assert cr["status"] == "awaiting_approval"


def test_retry_after_conflict_fixed(shop) -> None:
    c = shop.client
    dev = shop.user("alice")
    commit_and_push(shop.dev_clone, {"app/main.py": "# feature\n"}, "feature", "feature/r")
    report = local_report(shop, "feature/r")
    git(shop.dev_clone, "checkout", "-q", "main")
    commit_and_push(shop.dev_clone, {"app/main.py": "# main\n"}, "main", "main")
    cr = c.post("/api/v1/change-requests", json=deploy_request(shop, "feature/r", "dev", report=report), headers=dev).json()
    assert c.get(f"/api/v1/change-requests/{cr['id']}", headers=dev).json()["status"] == "conflict"
    # developer resolves locally and pushes a new commit, then sends a new request
    git(shop.dev_clone, "checkout", "-q", "feature/r")
    git(shop.dev_clone, "merge", "-q", "-X", "theirs", "origin/main", "-m", "merge main")
    git(shop.dev_clone, "push", "-q", "origin", "feature/r")
    cr2 = c.post("/api/v1/change-requests", json=deploy_request(shop, "feature/r", "dev"), headers=dev).json()
    assert c.get(f"/api/v1/change-requests/{cr2['id']}", headers=dev).json()["status"] == "deployed"
