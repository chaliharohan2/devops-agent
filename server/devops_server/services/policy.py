"""Deterministic policy: what the server agent may do on its own.

The LLM never makes these decisions. It can *raise* the risk level of a change
(its review is advisory and folded in with ``max``) but it can never lower the
deterministic floor computed here, and it can never approve anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from devops_common.protocol import ChangeReport, ConfigChangeKind, RiskLevel

PROD_NAMES = {"prod", "production", "live", "prd"}
_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}


def max_risk(*levels: RiskLevel | None) -> RiskLevel:
    present = [lv for lv in levels if lv is not None]
    return max(present, key=lambda r: _RANK[r]) if present else RiskLevel.MEDIUM


def is_production(env_name: str) -> bool:
    return env_name.lower() in PROD_NAMES


@dataclass
class RiskAssessment:
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)


def deterministic_risk(report: ChangeReport, env_name: str, discrepancies: list[str]) -> RiskAssessment:
    reasons: list[str] = []
    level = RiskLevel.LOW
    kinds = {c.kind for c in report.config_changes}

    def bump(to: RiskLevel, why: str) -> None:
        nonlocal level
        level = max_risk(level, to)
        reasons.append(why)

    if discrepancies:
        bump(RiskLevel.HIGH, "local and server analyses disagree")
    if report.requires_migration:
        bump(RiskLevel.HIGH if is_production(env_name) else RiskLevel.MEDIUM, "database migration")
    if ConfigChangeKind.ENV_VAR_REMOVED in kinds:
        bump(RiskLevel.MEDIUM, "environment variables removed")
    if ConfigChangeKind.ENV_VAR_ADDED in kinds:
        bump(RiskLevel.MEDIUM, "new environment variables")
    if kinds & {ConfigChangeKind.DEPENDENCY_CHANGED, ConfigChangeKind.BUILD_CHANGED}:
        bump(RiskLevel.MEDIUM, "dependencies/build changed")
    if ConfigChangeKind.MANIFEST_CHANGED in kinds:
        bump(RiskLevel.MEDIUM, "component manifest changed")
    if any("Possible" in w and "committed" in w for w in report.warnings):
        bump(RiskLevel.HIGH, "possible secret committed to the repository")
    if any("migration" in w.lower() and ("modified" in w or "deleted" in w) for w in report.warnings):
        bump(RiskLevel.HIGH, "existing migration modified/deleted")
    if is_production(env_name):
        bump(max_risk(level, RiskLevel.MEDIUM), "production environment")
    if not reasons:
        reasons.append("code-only change")
    return RiskAssessment(level=level, reasons=reasons)


@dataclass
class ApprovalDecision:
    requires_human: bool
    reasons: list[str]


def approval_decision(
    *, env_requires_approval: bool, env_name: str, risk: RiskLevel, missing_config: list[str], discrepancies: list[str]
) -> ApprovalDecision:
    reasons: list[str] = []
    if env_requires_approval:
        reasons.append(f"environment '{env_name}' requires approval")
    if is_production(env_name) and not env_requires_approval:
        reasons.append("production environments always require approval")
    if risk == RiskLevel.HIGH:
        reasons.append("high risk change")
    if missing_config:
        reasons.append(f"missing config values: {', '.join(missing_config)}")
    if discrepancies:
        reasons.append("analysis discrepancies need a human look")
    return ApprovalDecision(requires_human=bool(reasons), reasons=reasons)


def compare_reports(local: ChangeReport, server: ChangeReport) -> list[str]:
    """Differences between the developer agent's analysis and the server's own."""
    out: list[str] = []
    if local.head_sha != server.head_sha:
        out.append(f"head sha mismatch: local {local.head_sha[:10]} vs server {server.head_sha[:10]}")
    lf, sf = set(local.fingerprint()["config"]), set(server.fingerprint()["config"])
    for item in sorted(sf - lf):
        out.append(f"server found {item} which the local agent did not report")
    for item in sorted(lf - sf):
        out.append(f"local agent reported {item} which the server could not confirm")
    if local.requires_migration != server.requires_migration:
        out.append("migration requirement differs between analyses")
    return out
