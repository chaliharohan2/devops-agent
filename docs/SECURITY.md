# Security model and guardrails

The fleet's rules from the product brief, and exactly where each one is enforced:

| Rule | Enforcement (code) | Tested in |
|---|---|---|
| Local agents **cannot commit or push** | `ReadOnlyGit` allow‑list: only inspection subcommands (`log`, `diff`, `show`, `status`, `fetch`, `rev-parse`, `ls-files`, `grep`, `bundle create`, …). `commit`, `push`, `merge`, `rebase`, `reset`, `checkout`, `add`, `stash`, `tag`, branch creation, config writes, `--output`, `--upload-pack`, `-c` … raise `GitPolicyError` before any process runs | `packages/common/tests/test_gitguard.py::test_read_only_git_denies_writes`, `local_agent/tests/test_local_agent.py::test_local_agent_never_pushes` |
| Local agents never modify the developer's working tree | Test environments **mount** the checkout; other components come from agent‑owned clones under `~/.devops-agent/workspaces` (`WorkspaceGit` refuses paths outside that root) exported with `git archive` | `test_workspace_git_confined_to_workspace` |
| Only the developer pushes; deploys use what is pushed | Preflight blocks branches that are not on the remote; analysis runs on `origin/<branch>`; the server re‑verifies `head_sha ∈ origin/<branch>` | `test_preflight_blocks_unpushed_branch_and_warns`, `test_unpushed_commit_is_rejected` |
| Server agent **can merge but cannot change code** | `MergeGit` can only `merge --no-ff` (no conflict resolution, no `add`/`commit`), push only `HEAD:refs/heads/<branch>` (no force, no delete). `merge_verified()` checks the merge's changed paths ⊆ the developer's changed paths and the parents are exactly `[target, source]`, else aborts | `test_merge_git_push_restrictions`, `test_merge_verified_*`, `test_conflict_is_reported_never_resolved` |
| Server agent **can update configs** | Config is stored in Postgres (`config_entries`), not in repos; keys validated (`UPPER_SNAKE_CASE`), versioned with masked history | `test_missing_config_waits_then_maintainer_unblocks`, `test_invalid_config_key_rejected` |
| Server never trusts the laptop | Server re‑runs the analyzer on its own clone and diffs fingerprints with the local report; any discrepancy ⇒ risk `high` + human approval | `test_discrepancy_between_local_and_server_needs_human` |
| LLM output cannot cause damage | LLM review is schema‑validated and *advisory*: final risk = `max(deterministic floor, LLM)`; approvals are policy + humans only; operator chat tools are read‑only | `test_llm_review_is_advisory_and_cannot_lower_risk` |
| Humans approve risky changes | `approval_decision()`: env `requires_approval`, any prod‑named env, risk `high`, missing config, discrepancies. Approver must be maintainer/admin and **not the requester** | `test_prod_requires_approval_and_no_self_approval` |
| Outward actions from the local SLM need the developer's "yes" | Tools `request_deploy`, `request_peer_code`, `teardown_test_env` are `mutating=True`; the loop stops and asks for confirmation | `test_brain_llm_mutating_tool_waits_for_yes`, `test_brain_rules_fallback_deploy_needs_confirmation` |
| Secrets never reach laptops | `/spec` resolves config with `include_secrets=False` (keys only); the local agent stubs them with a placeholder; secrets encrypted at rest (Fernet), masked in API, plans and history | `test_environment_spec_never_contains_secrets`, `test_full_pipeline_merges_configures_and_deploys` |
| Peer sharing only with consent | Only the asked developer can approve/upload; only the requester can download; sha256 verified; artifacts expire (24h) | `test_peer_sharing_flow`, `test_peer_working_tree_share_includes_uncommitted_work` |
| Everything is auditable | `audit_events` rows for every agent action and human decision (create, analyze, approve/reject, merge, config update, deploy, peer events) | `test_full_pipeline_merges_configures_and_deploys` |

## Authentication & authorisation

* **Tokens**: random 256‑bit `dxa_…` bearer tokens, stored as SHA‑256 hashes. Users get personal tokens; each
  registered local agent gets its own token bound to the user (`api_tokens.agent_id`), so an agent can be revoked
  without touching the user.
* **Roles**: `developer` (request deploys, view, test specs, peers), `maintainer` (+ approve/reject, edit config,
  manage apps/environments, manual deploys), `admin` (+ users/tokens).
* **Bootstrap**: `DEVOPS_BOOTSTRAP_ADMIN_TOKEN` registers the first admin; rotate it afterwards.
* **Planned**: OIDC/SSO login for the dashboard, token expiry/rotation, mTLS between agents and server.

## Local daemon

* binds to `127.0.0.1` only (the CLI refuses other hosts);
* state‑changing requests require the `X-DevOps-Agent: 1` header and a localhost `Origin` — a custom header
  forces a CORS preflight that the daemon never grants to foreign origins, which blocks CSRF from websites;
* `~/.devops-agent/config.yaml` (contains the agent token) is written with mode `0600`.

## Git transport hardening

`gitguard` runs git with `core.hooksPath=/dev/null`, `core.fsmonitor=false`, `GIT_TERMINAL_PROMPT=0`,
`GIT_OPTIONAL_LOCKS=0` (so `status` never writes the index) and `GIT_ALLOW_PROTOCOL=file:git:http:https:ssh`
(blocks `ext::` command transports). Flags that write files or run programs (`--output`, `--exec`,
`--upload-pack`, `--receive-pack`, `--ext-diff`, `-c`, `--config`, `clone -u`) are rejected.

## Server‑side secrets

* `DEVOPS_SECRET_KEY` (Fernet) encrypts secret config values and secret values inside pending change requests.
  **Set it in production**; the development fallback derives a key from the DB URL.
* The rendered Compose file for a deployment contains decrypted secrets and is written with mode `0600`; the
  copy stored in the database (`deployments.plan`) is masked.
* Committed secrets are detected in diffs (AWS keys, private keys, GitHub tokens, hard‑coded passwords) and make
  the change high‑risk.

## Threats considered and residual risks

| Threat | Mitigation | Residual |
|---|---|---|
| Compromised laptop sends a forged report | server re‑analysis + discrepancy gate + pushed‑commit verification | attacker with push rights can still push malicious code — same as without the agent; branch protection and code review still apply |
| Prompt injection via commit messages / code comments reaching the server LLM | LLM only produces advisory text and a risk that cannot lower the floor; no LLM tool can mutate state | misleading summaries are possible; reviewers see the raw report too |
| Malicious peer artifact | consent, size limit, sha256, extracted with `tarfile` `data` filter (no absolute paths/links), runs only in the requester's local Docker | the code itself is untrusted — same as checking out a colleague's branch |
| Server host compromise | secrets encrypted at rest; tokens hashed | the server holds git push credentials and the Docker socket — treat it as production infrastructure |
| Docker socket on the server | required for the `compose` target | use a dedicated deploy host or the (planned) Kubernetes target with scoped credentials |
