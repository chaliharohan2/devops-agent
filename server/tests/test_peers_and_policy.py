from __future__ import annotations

import hashlib

from devops_common.protocol import ChangeReport, ConfigChange, ConfigChangeKind, RiskLevel
from devops_server.services import policy


def test_peer_sharing_flow(shop) -> None:
    c = shop.client
    alice = shop.user("alice")
    bob = shop.user("bob")
    carol = shop.user("carol")
    r = c.post("/api/v1/peers", headers=alice, json={"app": "shop", "component": "backend", "to_user": "bob",
                                                     "share": "branch", "ref": "feature/x", "message": "need your API"})
    assert r.status_code == 201, r.text
    pr = r.json()
    assert pr["status"] == "pending"
    # bob's agent sees it in the inbox
    assert c.get("/api/v1/agents/inbox", headers=bob).json()[-1]["kind"] == "peer_request"
    # only bob can respond / upload; carol cannot even see it
    assert c.post(f"/api/v1/peers/{pr['id']}/respond?approve=true", headers=alice).status_code == 409
    assert c.post(f"/api/v1/peers/{pr['id']}/respond?approve=true", headers=carol).status_code == 404
    assert c.put(f"/api/v1/peers/{pr['id']}/artifact", content=b"x", headers=bob).status_code == 409  # not approved
    assert c.post(f"/api/v1/peers/{pr['id']}/respond?approve=true", headers=bob).json()["status"] == "approved"
    payload = b"fake git bundle"
    up = c.put(f"/api/v1/peers/{pr['id']}/artifact", content=payload, headers={**bob, "X-Head-Sha": "abc123"})
    assert up.status_code == 200 and up.json()["artifact_sha256"] == hashlib.sha256(payload).hexdigest()
    assert c.get(f"/api/v1/peers/{pr['id']}/artifact", headers=bob).status_code == 409  # only requester downloads
    dl = c.get(f"/api/v1/peers/{pr['id']}/artifact", headers=alice)
    assert dl.status_code == 200 and dl.content == payload
    assert dl.headers["X-Head-Sha"] == "abc123"
    assert any(e["kind"] == "peer_ready" for e in c.get("/api/v1/agents/inbox", headers=alice).json())


def test_peer_request_validation(shop) -> None:
    c = shop.client
    alice = shop.user("alice")
    bad = [
        {"app": "shop", "component": "backend", "to_user": "nobody", "share": "branch", "ref": "x"},
        {"app": "shop", "component": "backend", "to_user": "alice", "share": "branch", "ref": "x"},
        {"app": "shop", "component": "nope", "to_user": "admin", "share": "branch", "ref": "x"},
        {"app": "shop", "component": "backend", "to_user": "admin", "share": "branch"},
    ]
    for body in bad:
        assert c.post("/api/v1/peers", headers=alice, json=body).status_code == 400


def _report(*changes: ConfigChange, migration: bool = False, warnings=()) -> ChangeReport:
    return ChangeReport(app="a", component="c", source_branch="f", target_branch="main", base_sha="b", head_sha="h",
                        config_changes=list(changes), requires_migration=migration, warnings=list(warnings))


def test_deterministic_risk_floor() -> None:
    assert policy.deterministic_risk(_report(), "dev", []).level == RiskLevel.LOW
    added = ConfigChange(kind=ConfigChangeKind.ENV_VAR_ADDED, component="c", key="X")
    assert policy.deterministic_risk(_report(added), "dev", []).level == RiskLevel.MEDIUM
    assert policy.deterministic_risk(_report(migration=True), "prod", []).level == RiskLevel.HIGH
    assert policy.deterministic_risk(_report(), "production", []).level == RiskLevel.MEDIUM
    assert policy.deterministic_risk(_report(), "dev", ["x"]).level == RiskLevel.HIGH
    leaked = _report(warnings=["Possible AWS access key id committed in a.py:1. Move it to the config store."])
    assert policy.deterministic_risk(leaked, "dev", []).level == RiskLevel.HIGH


def test_approval_decision() -> None:
    ok = policy.approval_decision(env_requires_approval=False, env_name="dev", risk=RiskLevel.MEDIUM,
                                  missing_config=[], discrepancies=[])
    assert not ok.requires_human
    prod = policy.approval_decision(env_requires_approval=False, env_name="prod", risk=RiskLevel.LOW,
                                    missing_config=[], discrepancies=[])
    assert prod.requires_human
    assert policy.max_risk(RiskLevel.LOW, RiskLevel.HIGH, None) == RiskLevel.HIGH
