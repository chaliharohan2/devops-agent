# Development guide

## Prerequisites

| Tool | Version | Used for |
|---|---|---|
| Python | 3.11 | both agents (`uv` installs it if missing) |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.5 | workspace/dependency management |
| Node.js | 22 LTS | building the web UI |
| Docker + Compose v2 | recent | test environments, deployments, Postgres |
| git | ≥ 2.40 | everything |
| Ollama (optional) | latest | local model for the developer agent |

## Install

```bash
uv sync --all-packages          # .venv with devops-common, devops-server, devops-local-agent + dev tools
(cd web && npm install && npm run build)
```

## Run the server agent

```bash
docker run -d --name devops-pg -e POSTGRES_USER=devops -e POSTGRES_PASSWORD=devops \
  -e POSTGRES_DB=devops_agent -p 127.0.0.1:5432:5432 postgres:16-alpine
export DATABASE_URL=postgresql+psycopg://devops:devops@127.0.0.1:5432/devops_agent
export DEVOPS_SECRET_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export DEVOPS_BOOTSTRAP_ADMIN_TOKEN=dxa_local_admin
export VLLM_BASE_URL=... VLLM_API_KEY=... VLLM_MODEL=...      # optional
uv run devops-server migrate
uv run devops-server serve --reload                          # http://127.0.0.1:8080 (dashboard + /docs)
uv run devops-server create-user alice --role developer      # prints a token
uv run devops-server check-llm                               # verify vLLM
```

All server settings: [`server/devops_server/settings.py`](../server/devops_server/settings.py)
(prefix `DEVOPS_`, plus `DATABASE_URL` and `VLLM_*`).

Register an app (or use the dashboard / `scripts/demo.sh`):

```bash
TOKEN=dxa_local_admin
curl -X POST localhost:8080/api/v1/apps -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"shop","environments":[{"name":"dev","branch":"develop"},{"name":"prod","branch":"main","requires_approval":true}]}'
curl -X POST localhost:8080/api/v1/apps/shop/components/import -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"repo_url":"git@github.com:acme/shop-backend.git","branch":"develop"}'
```

The server needs git credentials that can fetch and push the environment branches (SSH key or HTTPS token in the
container/user running it).

## Run a local agent

```bash
uv run devops-agent setup --server http://127.0.0.1:8080 --token <alice-token>
uv run devops-agent link shop/backend ~/code/shop-backend
uv run devops-agent model pull            # optional: Ornith-1.5-9B via Ollama
uv run devops-agent daemon                # developer app at http://127.0.0.1:7600
```

Use `DEVOPS_AGENT_HOME=/some/dir` to run several agents (e.g. two developers) on one machine. The daemon serves the
UI from `$DEVOPS_AGENT_UI_DIR`, `~/.devops-agent/ui` or the repo's `web/dist` (first one found).

## Web UI development

```bash
cd web && npm run dev     # http://localhost:5173 — proxies /api/v1 -> :8080 (server) and /api -> :7600 (local)
```

`?mode=server` forces the dashboard when both backends are reachable. The production build in `web/dist` is
served automatically by both agents.

## Tests and lint

```bash
uv run ruff check .
uv run pytest                                   # all suites, SQLite for server tests
TEST_DATABASE_URL=postgresql+psycopg://devops:devops@127.0.0.1:5432/devops_agent_test \
  uv run pytest server/tests local_agent/tests  # against PostgreSQL
(cd web && npm run build)                       # type-check + bundle
```

See [TESTING.md](TESTING.md) for what is covered.

## Database migrations

Models: `server/devops_server/models.py`. After changing them:

```bash
cd server && uv run alembic revision --autogenerate -m "describe change" && uv run alembic upgrade head
```

## End‑to‑end demo

```bash
scripts/demo.sh        # ~3–6 min on first run (image builds)
scripts/demo.sh down
```

`DEMO_DEPLOY_TARGET=dry_run` skips real deployments on the server side.

## Behind a corporate proxy

* Docker builds inside test environments need registry access. Set `build_network: host` in
  `~/.devops-agent/config.yaml` (local agent) or `DEVOPS_DEPLOY_BUILD_NETWORK=host` (server) if builds can only
  reach the internet through the host network.
* TLS‑intercepting proxies require the proxy CA inside images; add it to your base images or Dockerfiles.

## Code map

```
packages/common/devops_common/   protocol.py manifest.py gitguard.py analysis.py compose.py llm.py agentloop.py testing.py
server/devops_server/            main.py settings.py db.py models.py security.py schemas.py cli.py
  api/                           routes_admin.py routes_apps.py routes_changes.py routes_peers.py routes_chat.py deps.py
  services/                      orchestrator.py policy.py config_store.py deployer.py env_spec.py git_service.py
                                 llm_service.py peers.py audit.py jobs.py
local_agent/devops_local/        cli.py daemon.py service.py brain.py changes.py envbuilder.py peers.py repos.py
                                 hardware.py model_manager.py server_client.py config.py errors.py
web/src/                         App.tsx api.ts ui.tsx types.ts dev/* server/* styles.css
```

Conventions: Python 3.11, type hints everywhere, ruff (line length 110), Pydantic v2, SQLAlchemy 2.0 typed ORM.
New git operations must go through `devops_common.gitguard` — never call `git` directly from agent code.
