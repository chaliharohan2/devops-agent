# Testing

## Automated suites

| Suite | Path | What it covers |
|---|---|---|
| Shared library | `packages/common/tests` | git guard allow‑lists (17 forbidden operations), workspace confinement, verified merges + conflicts against real repos; analyzer (env vars in code, pydantic settings, `.env.example`, migrations, deps, moved vars, committed secrets, diff parsing); compose rendering for test and deploy; manifest inference; LLM client (base URL, `<think>` stripping, JSON repair, text tool calls, confirmation gating) |
| Server agent | `server/tests` | full pipeline against real git remotes: auth/roles, auto‑approve → merge → config → deploy, missing config → maintainer unblocks, prod approval + no self‑approval, conflicts never resolved, unpushed commits rejected, local/server discrepancy → human, secrets never in env specs, config key validation, manifest import, LLM review advisory (mocked vLLM), retry after conflict; peer flow + permissions; policy unit tests |
| Local agent | `local_agent/tests` | hardware → model recommendation, URL normalisation, auto‑linking, preflight (unpushed/uncommitted), never pushes, deploy end‑to‑end through an in‑process server, cwd resolution, test env rendering (local mount + cloned component + templates + stable ports), peer working‑tree and bundle sharing, brain fallback with confirmation, LLM tool calling (mocked Ollama), daemon CSRF guard |

Run: `uv run pytest` (SQLite) and with `TEST_DATABASE_URL=postgresql+psycopg://…` (PostgreSQL). CI runs both,
plus `ruff`, Alembic migrations and the web build (`.github/workflows/ci.yml`).

## End‑to‑end (real Docker)

`scripts/demo.sh` was run end‑to‑end during development:

1. server agent on PostgreSQL with Alembic migrations;
2. baseline **Compose deployment** of `shop` (Postgres + FastAPI backend + React frontend) — real images;
3. developer agent analyses a pushed branch (new `DISCOUNT_PERCENT` setting + Alembic migration), sends the deploy
   request; the server verifies, re‑analyses, reviews, auto‑approves (risk medium, dev), merges, bumps config to v2
   and redeploys — the migration runs and the dev API serves discounted prices;
4. developer builds a **local test environment** — her backend mounted with `--reload`, the dev frontend from an
   agent‑owned clone, Postgres; health checks pass; the API answers with the dev config applied.

The packaged server stack (`deploy/docker-compose.yml`) was also smoke‑tested (migrations, bootstrap admin,
dashboard, docker/compose/git available in the container).

## UI checks

The developer app and dashboard were exercised with Playwright (headless Chromium) against the demo: every page,
in‑app navigation, the deploy analysis flow, the chat fallback, desktop and 390 px mobile in dark mode. Screenshots
are in [`docs/images`](images/).

## Local model acceptance test (run on a standard 20 GB laptop)

Prerequisites: Ollama running, `devops-agent model pull`, a linked checkout with a pushed feature branch, and a
reachable server. Run each prompt with `devops-agent chat "<prompt>"` and check the first tool the agent selects
(shown under "tool calls" in the app). Target: **≥ 18/20 correct**, decode ≥ 8 tok/s, `ollama` RSS ≤ 8 GB while a
test environment is up.

| # | Prompt | Expected tool |
|---|---|---|
| 1 | what does my branch change for staging? | `analyze_changes(environment=staging)` |
| 2 | do I need any new config before deploying to dev? | `analyze_changes(environment=dev)` |
| 3 | deploy my changes to dev | `request_deploy(dev)` → asks for confirmation |
| 4 | ship feature/payments to staging with PAYMENT_URL=https://pay.test | `request_deploy(staging, branch, config)` → confirmation |
| 5 | set up an e2e env for my backend | `setup_test_env(local_components=[backend])` |
| 6 | spin up the frontend against dev so I can test it | `setup_test_env(environment=dev, local_components=[frontend])` |
| 7 | I want to test both my frontend and backend changes together | `setup_test_env(local_components=[frontend, backend])` |
| 8 | which test environments are running? | `test_env_status` |
| 9 | is shop-dev-backend healthy? | `test_env_status(name=shop-dev-backend)` |
| 10 | tear down shop-dev-backend | `teardown_test_env` → confirmation |
| 11 | what's the status of my deploys? | `deploy_status` |
| 12 | why did request 12 fail? | `deploy_status(id=12)` |
| 13 | get bob's frontend branch feature/new-ui | `request_peer_code(from_user=bob, branch=feature/new-ui)` → confirmation |
| 14 | can I test with carol's current backend code? | `request_peer_code(from_user=carol)` → confirmation |
| 15 | push my branch for me | **no tool**; explains it never pushes and gives `git push` |
| 16 | commit these changes and deploy | **no tool for commit**; tells the developer to commit/push first |
| 17 | deploy to prod | `request_deploy(prod)` → confirmation; mentions approval is required |
| 18 | what config does the payments change need in staging? | `analyze_changes(staging)` |
| 19 | restart my test env with my latest dependency changes | `setup_test_env` |
| 20 | hi, what can you do? | **no tool**; short capability summary |
