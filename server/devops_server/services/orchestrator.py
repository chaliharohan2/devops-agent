"""The server agent's change-request pipeline.

    received ─► analyzing ─► (awaiting_approval) ─► approved ─► merging ─► merged ─► deploying ─► deployed
                     │                 │                            │                    └────► failed
                     └──► failed       └──► rejected                └──► conflict

1. **Verify** the commit the developer's agent reported is really pushed to the
   source branch (the server never trusts the client's view of git).
2. **Analyse independently** with the same deterministic analyzer and diff the
   result against the local agent's report (discrepancies => human review).
3. **Review** with the LLM (advisory: summary, concerns, rollout steps) on top of
   a deterministic risk floor.
4. **Policy** decides whether a human must approve (prod, high risk, missing config...).
5. **Merge** with :class:`MergeGit` — ``--no-ff``, no conflict resolution, and a
   proof that the merge only contains the developer's changes. Push.
6. **Configure**: apply the proposed config values to the store (new version).
7. **Deploy** if the environment auto-deploys.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from devops_common.analysis import analyze_branch
from devops_common.gitguard import GitCommandError, GitPolicyError
from devops_common.protocol import (
    ChangeReport,
    ConfigChangeKind,
    ConfigValue,
    ReviewResult,
    RiskLevel,
)
from devops_common.protocol import (
    ChangeRequestStatus as S,
)

from ..db import session_scope
from ..models import ChangeRequest, Deployment
from ..security import decrypt
from . import audit, config_store, deployer, env_spec, git_service, llm_service, policy

log = logging.getLogger(__name__)


def _set(cr: ChangeRequest, status: S, message: str = "") -> None:
    cr.status = status.value
    cr.timeline = [*(cr.timeline or []), {"at": datetime.now(UTC).isoformat(), "status": status.value, "message": message}]


def _notify(db: Session, cr: ChangeRequest, message: str) -> None:
    audit.notify(db, cr.requested_by_id, "change_request_update", id=cr.id, status=cr.status, message=message,
                 app=cr.app.name, environment=cr.environment.name, component=cr.component.name)


def proposed_values(cr: ChangeRequest) -> list[ConfigValue]:
    out = []
    for raw in cr.proposed_config or []:
        v = dict(raw)
        if v.get("is_secret") and v.get("value") is not None:
            v["value"] = decrypt(v["value"])
        out.append(ConfigValue.model_validate(v))
    return out


def missing_config(db: Session, cr: ChangeRequest, report: ChangeReport) -> list[str]:
    proposed = {(v.key) for v in proposed_values(cr) if v.value is not None}
    missing = []
    for c in report.config_changes:
        if c.kind != ConfigChangeKind.ENV_VAR_ADDED or not c.key or not c.requires_value:
            continue
        if c.key in proposed or config_store.has_key(db, cr.environment, c.key, cr.component.name):
            continue
        missing.append(c.key)
    return sorted(set(missing))


# --------------------------------------------------------------------------- #
# Stage 1-4: analysis and decision
# --------------------------------------------------------------------------- #


def process_change_request(cr_id: int) -> None:
    with session_scope() as db:
        cr = db.get(ChangeRequest, cr_id)
        if cr is None:
            return
        _set(cr, S.ANALYZING, "verifying the pushed commit and analysing the change")
    try:
        with session_scope() as db:
            cr = db.get(ChangeRequest, cr_id)
            assert cr is not None
            comp, env = cr.component, cr.environment
            if not comp.repo_url:
                raise ValueError(f"component {comp.name} has no repo_url configured on the server")
            with git_service.lock_for(comp.repo_url):
                g = git_service.read_git(comp.repo_url, force_sync=True)
                if not git_service.synced_merge_git(comp.repo_url).commit_on_branch(cr.head_sha, cr.source_branch):
                    _set(cr, S.FAILED, "commit not found on the pushed branch")
                    cr.error = (f"Commit {cr.head_sha[:10]} is not on origin/{cr.source_branch}. "
                                "Push your branch (the agents never push for you) and try again.")
                    _notify(db, cr, cr.error)
                    return
                if g.try_rev_parse(f"refs/remotes/origin/{cr.target_branch}") is None:
                    raise ValueError(f"target branch {cr.target_branch!r} does not exist on the remote")
                manifest = env_spec.component_manifest_at(comp, cr.head_sha)
                server_report = analyze_branch(
                    g, app=cr.app.name, component=manifest.model_copy(update={"path": comp.path}),
                    base_ref=f"refs/remotes/origin/{cr.target_branch}", head_ref=cr.head_sha,
                    source_branch=cr.source_branch, target_branch=cr.target_branch, repo_url=comp.repo_url,
                )
            local_report = ChangeReport.model_validate(cr.local_report)
            discrepancies = policy.compare_reports(local_report, server_report)
            missing = missing_config(db, cr, server_report)
            floor = policy.deterministic_risk(server_report, env.name, discrepancies)
            review = llm_service.review_change(server_report, env.name, discrepancies, missing, floor.level, floor.reasons)
            risk = policy.max_risk(floor.level, review.risk_level)
            review.risk_level = risk
            decision = policy.approval_decision(
                env_requires_approval=env.requires_approval, env_name=env.name, risk=risk,
                missing_config=missing, discrepancies=discrepancies,
            )
            cr.server_report = server_report.model_dump(mode="json")
            cr.discrepancies = discrepancies
            cr.missing_config = missing
            cr.review = review.model_dump(mode="json")
            cr.risk_level = risk.value
            cr.approval_reasons = decision.reasons
            audit.record(db, audit.SERVER_AGENT, "change_request.analyzed", "change_request", cr.id,
                         risk=risk.value, review_source=review.source, discrepancies=discrepancies, missing=missing)
            if decision.requires_human:
                _set(cr, S.AWAITING_APPROVAL, "; ".join(decision.reasons))
                _notify(db, cr, "Waiting for approval: " + "; ".join(decision.reasons))
                return
            _set(cr, S.APPROVED, f"auto-approved by policy (risk {risk.value})")
            cr.approved_by = audit.SERVER_AGENT
    except (GitCommandError, GitPolicyError, ValueError, OSError) as e:
        log.exception("analysis failed for change request %s", cr_id)
        _fail(cr_id, f"analysis failed: {e}")
        return
    execute_change_request(cr_id)


# --------------------------------------------------------------------------- #
# Stage 5-7: merge, configure, deploy
# --------------------------------------------------------------------------- #


def execute_change_request(cr_id: int) -> None:
    try:
        with session_scope() as db:
            cr = db.get(ChangeRequest, cr_id)
            assert cr is not None
            if cr.status != S.APPROVED.value:
                return
            _set(cr, S.MERGING, f"merging {cr.source_branch} into {cr.target_branch}")
        with session_scope() as db:
            cr = db.get(ChangeRequest, cr_id)
            assert cr is not None
            url = cr.component.repo_url
            assert url
            message = (f"Merge {cr.source_branch} into {cr.target_branch}\n\n"
                       f"Requested by {cr.requested_by.username} via DevOps Agent (change request #{cr.id}).")
            with git_service.lock_for(url):
                mg = git_service.synced_merge_git(url, force=True)
                result = mg.merge_verified(cr.head_sha, cr.target_branch, message)
                if result.ok and not result.already_merged:
                    mg.push_merge(cr.target_branch)
            if not result.ok:
                report = ChangeReport.model_validate(cr.server_report or cr.local_report)
                explanation = llm_service.explain_conflict(report, result.conflict_files or [])
                cr.error = explanation
                _set(cr, S.CONFLICT, f"conflicts in {', '.join(result.conflict_files or [])}")
                audit.record(db, audit.SERVER_AGENT, "change_request.conflict", "change_request", cr.id,
                             files=result.conflict_files)
                _notify(db, cr, explanation)
                return
            cr.merge_sha = result.merge_sha
            _set(cr, S.MERGED, "already contained in target" if result.already_merged
                 else f"merged as {result.merge_sha[:10] if result.merge_sha else '?'}")
            audit.record(db, audit.SERVER_AGENT, "git.merge", "change_request", cr.id, repo=url,
                         target=cr.target_branch, merge_sha=result.merge_sha, source_sha=cr.head_sha,
                         target_before=result.target_before)
            # configs travel with the code they belong to
            values = [v for v in proposed_values(cr) if v.value is not None]
            for v in values:
                v.component = v.component or cr.component.name
            if values:
                version = config_store.apply(db, cr.environment, values, actor=f"{audit.SERVER_AGENT} (for {cr.requested_by.username})",
                                             message=f"change request #{cr.id}", change_request_id=cr.id)
                _set(cr, S.MERGED, f"config updated to version {version}")
            if not cr.environment.auto_deploy:
                _notify(db, cr, "Merged. This environment deploys manually.")
                return
            dep = Deployment(app_id=cr.app_id, environment_id=cr.environment_id, change_request_id=cr.id,
                             triggered_by=cr.requested_by.username,
                             component_shas={cr.component.name: cr.merge_sha} if cr.merge_sha else {})
            db.add(dep)
            db.flush()
            cr.deployment_id = dep.id
            _set(cr, S.DEPLOYING, f"deployment #{dep.id}")
        with session_scope() as db:
            cr = db.get(ChangeRequest, cr_id)
            assert cr is not None
            dep = db.get(Deployment, cr.deployment_id)
            assert dep is not None
            deployer.run_deployment(db, dep)
            if dep.status == "succeeded":
                _set(cr, S.DEPLOYED, f"deployment #{dep.id} succeeded ({dep.target})")
                _notify(db, cr, f"Deployed to {cr.environment.name}.")
            else:
                cr.error = dep.diagnosis or "deployment failed; see deployment log"
                _set(cr, S.FAILED, f"deployment #{dep.id} failed")
                _notify(db, cr, cr.error)
    except (GitCommandError, GitPolicyError, ValueError, OSError) as e:
        log.exception("execution failed for change request %s", cr_id)
        _fail(cr_id, f"{type(e).__name__}: {e}")


def _fail(cr_id: int, message: str) -> None:
    with session_scope() as db:
        cr = db.get(ChangeRequest, cr_id)
        if cr is None:
            return
        cr.error = message
        _set(cr, S.FAILED, message[:500])
        audit.record(db, audit.SERVER_AGENT, "change_request.failed", "change_request", cr.id, error=message[:2000])
        _notify(db, cr, message)


def approve(db: Session, cr: ChangeRequest, actor: str, *, override_missing: bool = False) -> None:
    if cr.status != S.AWAITING_APPROVAL.value:
        raise ValueError(f"change request is {cr.status}, not awaiting approval")
    report = ChangeReport.model_validate(cr.server_report or cr.local_report)
    still_missing = missing_config(db, cr, report)
    if still_missing and not override_missing:
        raise ValueError(f"set these config values first (or approve with override): {', '.join(still_missing)}")
    cr.missing_config = still_missing
    cr.approved_by = actor
    _set(cr, S.APPROVED, f"approved by {actor}")
    audit.record(db, actor, "change_request.approved", "change_request", cr.id, override_missing=override_missing)
    # The caller commits, then enqueues execute_change_request (the worker must see committed state).


def reject(db: Session, cr: ChangeRequest, actor: str, reason: str) -> None:
    if S(cr.status).terminal:
        raise ValueError(f"change request already {cr.status}")
    _set(cr, S.REJECTED, f"rejected by {actor}: {reason}")
    cr.error = reason
    audit.record(db, actor, "change_request.rejected", "change_request", cr.id, reason=reason)
    _notify(db, cr, f"Rejected by {actor}: {reason}")


def review_or_none(cr: ChangeRequest) -> ReviewResult | None:
    return ReviewResult.model_validate(cr.review) if cr.review else None


def risk_or_none(cr: ChangeRequest) -> RiskLevel | None:
    return RiskLevel(cr.risk_level) if cr.risk_level else None
