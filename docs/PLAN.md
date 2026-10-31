# Project plan

## Goal

Give every developer an agent that (1) gets their pushed work deployed safely and (2) builds a realistic
end‑to‑end test environment on their laptop in one step — while a single server agent keeps deployments,
merges and environment configuration consistent and auditable.

## Requirements → implementation

| # | Requirement (from the brief) | Status | Where |
|---|---|---|---|
| R1 | An app every developer downloads | ✅ web‑first: `devops-agent` CLI + localhost daemon serving the developer UI; desktop shell planned (Phase 3) | `local_agent/`, `web/` |
| R2 | It downloads a local model with access to the developer's files/code | ✅ hardware detection → model recommendation → `ollama pull` + tuned alias; agent reads linked checkouts (read‑only) | `devops_local/hardware.py`, `model_manager.py`, `repos.py` |
| R3 | Best model for 20 GB CPU RAM / 512 GB disk, researched | ✅ Ornith‑1.5‑9B Q4_K_M (Gemma 4 E4B fallback, Gemma 4 12B optional) | [research/LOCAL_MODEL_SELECTION.md](research/LOCAL_MODEL_SELECTION.md) |
| R4 | One main server agent managing all deployments and changes | ✅ | `server/` |
| R5 | Local agent checks committed + pushed code and works out which configs changed from the codebase structure | ✅ deterministic analyzer driven by `devops-agent.yaml` (or inferred structure) | `devops_common/analysis.py`, `manifest.py` |
| R6 | Local agent informs the server agent about config changes and branches to merge | ✅ `DeployRequest` | `devops_local/changes.py`, `POST /change-requests` |
| R7 | Server agent merges into the required branch and updates configs | ✅ verified `--no-ff` merge + push; versioned config store | `orchestrator.py`, `config_store.py` |
| R8 | Server agent can merge but cannot change code — only configs | ✅ enforced by `MergeGit` + merge verification | `gitguard.py`, [SECURITY.md](SECURITY.md) |
| R9 | Local agents cannot commit or push | ✅ enforced by `ReadOnlyGit` | `gitguard.py` |
| R10 | Local agent fetches configs + branches from the server, creates Docker containers per component, explains how to test e2e | ✅ `/spec` + environment builder + instructions | `envbuilder.py` |
| R11 | Developer‑to‑developer: get another developer's unpushed code and test with it (next phase) | 🟡 preview implemented (consent, bundle/working‑tree, relay, use as env source) | `peers.py` (both sides) |
| R12 | Server agent uses the provided vLLM endpoint | ✅ code + `check-llm`; ⚠️ endpoint not reachable from the build sandbox, see [SERVER_LLM.md](SERVER_LLM.md) | `llm_service.py` |
| R13 | Backend Python 3.11, Flask/FastAPI; Postgres + SQLAlchemy; React or Angular frontend | ✅ agents are Python 3.11/FastAPI/SQLAlchemy/Postgres; UI React; manifests/inference support FastAPI, Flask, React/Vite, Angular | |
| R14 | Build everything locally; app packaging later | ✅ | |
| R15 | Document everything; plans and technical docs in the repo; detailed README | ✅ | `docs/`, `README.md` |

## Assumptions made (please confirm — see open questions)

1. **Git hosting is generic.** The server merges with plain git (`fetch` → `merge --no-ff` → `push`), which works
   with GitHub, GitLab, Bitbucket or bare repos. It needs push rights on environment branches.
2. **Environments map to branches.** Each environment tracks one branch (per‑component overrides allowed);
   deploying to an environment means merging into its branch.
3. **Configuration moves into the server's config store** (environment variables + per‑component scope).
   Repo files like `.env.example` remain templates and are used for change detection.
4. **First deployment target is Docker Compose** on a host the server can reach via the Docker socket. A
   `dry_run` target exists for environments deployed by another system.
5. **Roles**: developer / maintainer / admin; production needs a maintainer who is not the requester.
6. **Secrets never go to laptops**; test environments use local values from manifests or placeholders.

## Phases

### Phase 0 — Foundations ✅ (this repository)
Monorepo, shared protocol, git capability guards, analyzer, compose renderer, LLM client, CI.

### Phase 1 — MVP ✅ (this repository)
* Server agent: pipeline, config store, approvals, compose/dry‑run deployer, audit, dashboard, operator chat.
* Local agent: CLI + daemon + developer UI, deploy flow, test environments, model management, SLM brain.
* Example app + one‑command demo (`scripts/demo.sh`) + 70 automated tests.

### Phase 1.5 — Pilot hardening (next 2–4 weeks)
| Item | Why |
|---|---|
| Run `devops-server check-llm` against the real vLLM; tune prompts/parsers | endpoint unreachable from the build sandbox |
| Model acceptance test on two standard laptops (macOS + Windows/WSL) | confirm tok/s and RAM with Docker running |
| Git hosting integration: credentials, optional **PR‑based merges** (GitHub/GitLab API) so branch protection & required reviews keep working | many teams forbid direct pushes to protected branches |
| Import real apps: write `devops-agent.yaml` for 1–2 services; tune analyzer rules | real‑world coverage |
| SSO (OIDC) for the dashboard; token expiry/rotation | security |
| Move background jobs to a queue (arq/RQ) + Postgres advisory locks | multiple server replicas |
| Rollback: "redeploy previous deployment" (SHAs + config version are already recorded) | operations |
| Notifications: Slack/Teams webhook on awaiting‑approval / failed | humans in the loop |

### Phase 2 — Developer‑to‑developer (preview → GA)
* consent UX in the app (desktop notification), selective sharing (component sub‑paths), larger repos (incremental
  bundles), optional LAN‑direct transfer, artifact cleanup job, audit views;
* "share my env": hand a teammate the same composed environment (spec + peer sources).

### Phase 3 — Desktop app
* Tauri shell around the existing daemon + UI (daemon frozen with PyInstaller), tray icon, auto‑start, auto‑update;
* first‑run wizard: server URL + token, Docker check, Ollama install/detect, model pull with progress;
* OS keychain for the agent token.

### Phase 4 — More deployment targets & environments
* Kubernetes/Helm target (render manifests from the same `EnvironmentSpec`), cloud targets;
* ephemeral **preview environments per branch** on the server (same spec, server‑side);
* canary/blue‑green, health‑gated rollouts, automatic rollback;
* secrets backends (Vault, AWS SSM, GCP Secret Manager) behind the config store interface.

## Open questions for you

1. **Git hosting** — GitHub, GitLab, Bitbucket or self‑hosted? Are environment branches protected (would you
   prefer the server agent to open and merge PRs instead of pushing merge commits)?
2. **Hosting today** — where do apps run (VMs with Docker, Kubernetes, a cloud PaaS)? This decides the Phase 4 target.
3. **Branch conventions** — e.g. `develop`→dev, `release/*`→staging, `main`→prod?
4. **Current config/secrets** — `.env` files, Vault, AWS SSM…? We can import them into the config store.
5. **Identity** — which SSO provider (Google, Okta, Azure AD)?
6. **Peer sharing policy** — is relaying working‑tree snapshots through the server acceptable for your data
   governance, or should it be LAN‑only / branch‑only?
7. **Approvers** — who may approve production deploys? Per app?
8. **Developer OS mix** — macOS / Windows / Linux? (Docker Desktop on macOS/Windows reserves RAM; affects the model budget.)
9. **vLLM** — is it started with `--enable-auto-tool-choice` and a tool‑call parser? Is it reachable from where the
   server agent will run?

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Small local model is slow/unreliable at tool calling on CPU | poor chat UX | deterministic fallback router; every feature also in UI/CLI without the model; swappable model |
| Direct pushes conflict with branch protection | merges blocked | PR‑based merge mode (Phase 1.5) |
| Analyzer misses a config change pattern | missing config at deploy | server re‑analysis, manifest `env` declarations, `required` warnings, LLM review "missing_config", deploy health checks |
| Docker Desktop memory pressure on 20 GB laptops | slow tests | model budget reserves 9 GB; envs only start needed components; Gemma 4 E4B fallback |
| Server agent host compromise | high | dedicated host, encrypted secrets, scoped git credentials, audit log, K8s target with RBAC later |
