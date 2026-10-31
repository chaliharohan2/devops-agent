"""Server agent reasoning via the vLLM endpoint.

The LLM is used where language understanding helps humans, never where a wrong
answer could cause damage:

* reviewing a change request (risk, concerns, rollout steps) — advisory; the
  deterministic policy floor always applies;
* explaining merge conflicts to the developer;
* diagnosing failed deployments from logs;
* answering operator questions in the dashboard chat using *read-only* tools.

Everything degrades gracefully to deterministic text when the LLM is unavailable.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from devops_common.llm import LLMClient, LLMError
from devops_common.protocol import ChangeReport, ConfigChangeKind, ReviewResult, RiskLevel

from ..settings import get_settings

SYSTEM_PROMPT = """You are the Server DevOps Agent for an engineering team.
You manage deployments and environment configuration. Hard rules you cannot break:
- You never modify application code. You may only merge branches developers pushed, and update environment configuration.
- You never approve your own high-risk actions; humans approve.
- You only state facts present in the provided data. If unsure, say so.
Be concise and concrete. Prefer bullet points a busy engineer can act on."""

REVIEW_PROMPT = """Review this change request before it is merged and deployed.
Use ONLY the facts below (they come from deterministic analysis of the git diff).
Return JSON with:
- risk_level: "low" | "medium" | "high"
- summary: 1-3 sentences on what the change does operationally
- concerns: specific risks (empty list if none)
- missing_config: config keys that must be set before deploying
- deployment_steps: ordered rollout steps (e.g. set config, run migration, deploy backend, smoke test)"""


@lru_cache
def get_client() -> LLMClient | None:
    s = get_settings()
    if not s.llm_configured:
        return None
    assert s.vllm_base_url and s.vllm_model
    return LLMClient(s.vllm_base_url, s.vllm_model, s.vllm_api_key, timeout=s.llm_timeout_s,
                     no_think_params=s.llm_no_think_params)


def health(probe: bool = False) -> dict[str, Any]:
    client = get_client()
    if client is None:
        return {"ok": False, "configured": False, "error": "VLLM_BASE_URL / VLLM_MODEL not set or LLM disabled"}
    info = client.health()
    info["configured"] = True
    if probe and info.get("ok"):
        try:
            res = client.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=20)
            info["probe"] = {"reply": res.content[:100], "latency_s": round(res.latency_s, 2)}
        except LLMError as e:
            info["probe"] = {"error": str(e)}
    return info


def _facts(report: ChangeReport, env_name: str, discrepancies: list[str], missing: list[str],
           floor: RiskLevel, floor_reasons: list[str]) -> str:
    return json.dumps(
        {
            "app": report.app,
            "component": report.component,
            "environment": env_name,
            "source_branch": report.source_branch,
            "target_branch": report.target_branch,
            "commits": [c.subject for c in report.commits][:20],
            "files_changed": [f"{f.status} {f.path} (+{f.additions}/-{f.deletions})" for f in report.changed_files][:60],
            "config_changes": [
                {"kind": c.kind, "key": c.key, "secret": c.is_secret_hint, "needs_value": c.requires_value,
                 "evidence": [f"{e.file}:{e.line}" for e in c.evidence][:2]}
                for c in report.config_changes
            ],
            "requires_rebuild": report.requires_rebuild,
            "requires_migration": report.requires_migration,
            "warnings": report.warnings,
            "analysis_discrepancies": discrepancies,
            "config_missing_in_environment": missing,
            "policy_risk_floor": floor,
            "policy_reasons": floor_reasons,
        },
        indent=1,
    )


def deterministic_review(report: ChangeReport, env_name: str, missing: list[str], floor: RiskLevel,
                         floor_reasons: list[str]) -> ReviewResult:
    steps: list[str] = []
    if missing:
        steps.append(f"Set config in '{env_name}': {', '.join(missing)}")
    steps.append(f"Merge {report.source_branch} into {report.target_branch}")
    if report.requires_rebuild:
        steps.append(f"Rebuild image for {report.component}")
    if report.requires_migration:
        steps.append("Run database migrations")
    steps.append(f"Deploy {report.component} to '{env_name}' and run health checks")
    removed = [c.key for c in report.config_changes if c.kind == ConfigChangeKind.ENV_VAR_REMOVED and c.key]
    if removed:
        steps.append(f"After rollout, consider removing unused config: {', '.join(removed)}")
    return ReviewResult(risk_level=floor, summary=report.summary, concerns=floor_reasons + report.warnings,
                        missing_config=missing, deployment_steps=steps, source="deterministic")


def review_change(report: ChangeReport, env_name: str, discrepancies: list[str], missing: list[str],
                  floor: RiskLevel, floor_reasons: list[str]) -> ReviewResult:
    client = get_client()
    fallback = deterministic_review(report, env_name, missing, floor, floor_reasons)
    if client is None:
        return fallback
    try:
        res = client.chat_json(SYSTEM_PROMPT + "\n\n" + REVIEW_PROMPT,
                               _facts(report, env_name, discrepancies, missing, floor, floor_reasons), ReviewResult)
        res.source = "llm"
        # The model may add missing keys but may not drop ones policy found.
        res.missing_config = sorted(set(res.missing_config) | set(missing))
        if not res.deployment_steps:
            res.deployment_steps = fallback.deployment_steps
        return res
    except LLMError as e:
        fallback.concerns.append(f"(LLM review unavailable: {e})")
        return fallback


def explain_conflict(report: ChangeReport, files: list[str]) -> str:
    base = (f"Merging {report.source_branch} into {report.target_branch} conflicts in: {', '.join(files) or 'unknown files'}. "
            f"The server agent never resolves code conflicts. Please merge or rebase {report.target_branch} into "
            f"your branch locally, resolve the conflicts, push, and request the deploy again.")
    client = get_client()
    if client is None:
        return base
    try:
        res = client.chat(
            [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user", "content": f"{base}\nCommits on the branch: {[c.subject for c in report.commits][:10]}.\n"
              "Write a short, friendly message to the developer with exact git commands to fix this."}],
            max_tokens=400,
        )
        return res.content or base
    except LLMError:
        return base


def diagnose_deployment(log_tail: str) -> str | None:
    client = get_client()
    if client is None:
        return None
    try:
        res = client.chat(
            [{"role": "system", "content": SYSTEM_PROMPT},
             {"role": "user", "content": "This deployment failed. From the log tail, give the most likely root cause "
              "and the next 1-3 steps. Do not invent details.\n\n```\n" + log_tail[-6000:] + "\n```"}],
            max_tokens=500,
        )
        return res.content
    except LLMError:
        return None
