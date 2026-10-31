# API reference

Both agents are FastAPI apps, so live interactive docs are available at `/docs` (Swagger) and `/openapi.json`.
The exported OpenAPI documents are committed under [`docs/openapi/`](openapi/) (regenerate with
`scripts/export_openapi.py`). Wire models shared by both sides live in
[`devops_common/protocol.py`](../packages/common/devops_common/protocol.py).

## Server agent — `/api/v1`

Auth: `Authorization: Bearer <token>`. Roles: **D** developer, **M** maintainer, **A** admin (each includes the
previous).

### System & identity

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/health` | — | liveness + DB check |
| GET | `/llm/health?probe=true` | D | vLLM reachability, models, optional "pong" probe with latency |
| GET | `/me` | D | current user (+ agent id when called with an agent token) |
| GET | `/users` | D | list users |
| POST | `/users` | A | create user → returns a one‑time token |
| POST | `/users/{username}/tokens` | D (self) / A | issue another token |

### Agents (used by local agents)

| Method | Path | Description |
|---|---|---|
| POST | `/agents/register` | body `AgentRegistration{hostname, version, model, capabilities}` → `AgentRegistered{agent_id, agent_token}` (call with the user's token) |
| POST | `/agents/heartbeat` | update model/version/capabilities; `last_seen_at` is updated on every call |
| GET | `/agents` | registered agents with online status |
| GET | `/agents/inbox?after=<id>` | notifications for the caller: `change_request_update`, `peer_request`, `peer_response`, `peer_ready` |

### Apps, components, environments

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/apps` | D | apps with components and environments |
| POST | `/apps` | M | create app (optionally with components and environments) |
| GET | `/apps/{app}` | D | one app |
| POST | `/apps/{app}/components` | M | add component `{name, kind, repo_url, path, manifest?}` |
| POST | `/apps/{app}/components/import` | M | `{repo_url, branch}` — read `devops-agent.yaml` from the repo and register/update its components |
| POST | `/apps/{app}/environments` | M | `{name, branch, branch_overrides, requires_approval, auto_deploy, deploy_target}` |
| PATCH | `/apps/{app}/environments/{env}` | M | update those fields |
| GET | `/apps/{app}/environments/{env}/spec` | D | **`EnvironmentSpec`** for local test envs: per component repo, branch, commit SHA, manifest, non‑secret config, secret *keys*; infrastructure services |

`deploy_target`: `{"type": "compose" | "dry_run", "host_ports": {"frontend": 8091, ...}}`.

### Configuration

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/apps/{app}/environments/{env}/configs` | D | entries (secrets masked) |
| PUT | `/apps/{app}/environments/{env}/configs` | M | `{values: [ConfigValue], deletes: [{key, component}], message}` → `{config_version}` |
| GET | `/apps/{app}/environments/{env}/configs/history` | D | versions with masked diffs, actor, linked change request |

`ConfigValue = {key, value, is_secret, component (null = app‑wide), description}`.

### Change requests (deploy requests)

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/change-requests` | D | body **`DeployRequest`** `{app, component, environment, source_branch, head_sha, report: ChangeReport, config_values, notes}` → 202 + `ChangeRequestView`; starts the pipeline |
| GET | `/change-requests?app=&status=&mine=&limit=` | D | list |
| GET | `/change-requests/{id}` | D | `ChangeRequestView` (status, risk, review, discrepancies, timeline, merge SHA, deployment id, error) |
| GET | `/change-requests/{id}/reports` | D | raw local and server `ChangeReport`s, missing config, approval reasons |
| POST | `/change-requests/{id}/approve` | M (not requester) | `{override_missing: bool}` |
| POST | `/change-requests/{id}/reject` | M | `{reason}` |
| POST | `/change-requests/{id}/retry` | requester / M | re‑run a `failed`/`conflict` request |

### Deployments

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/deployments?app=&environment=` | D | list |
| GET | `/deployments/{id}` | D | status, target, component SHAs, config version, masked plan, log, LLM diagnosis |
| POST | `/apps/{app}/environments/{env}/deploy` | M | redeploy current branches (e.g. after a config‑only change); optional `{component_shas}` pin |

### Peers (phase 2)

| Method | Path | Who | Description |
|---|---|---|---|
| POST | `/peers` | requester | `PeerRequestCreate{app, component, to_user, share: branch\|working_tree, ref, message}` |
| GET | `/peers` | both | requests I sent or received |
| POST | `/peers/{id}/respond?approve=true\|false` | asked developer | consent |
| PUT | `/peers/{id}/artifact` | asked developer | raw body (git bundle or tar.gz), header `X-Head-Sha` |
| GET | `/peers/{id}/artifact` | requester | download; headers `X-Artifact-Sha256`, `X-Head-Sha`, `X-Share-Kind` |

### Audit & operator chat

| Method | Path | Description |
|---|---|---|
| GET | `/audit?limit=&action=` | audit events (prefix filter on `action`, e.g. `change_request.`) |
| POST | `/agent/chat` | `{messages:[{role, content}]}` → `{reply, steps}`; vLLM with read‑only tools (`list_apps`, `list_change_requests`, `get_change_request`, `list_deployments`, `list_config_keys`, `recent_audit_events`) |

## Local agent daemon — `http://127.0.0.1:7600/api`

Used by the developer app UI (and handy for scripting). Non‑GET requests must send `X-DevOps-Agent: 1`.

| Method | Path | Description |
|---|---|---|
| GET | `/status` | user, server reachability, Docker, model status, linked repos, environments |
| GET | `/hardware` | detected hardware, recommended model, catalog |
| GET / POST | `/model`, `/model/pull {key?}` | model status / pull (returns a job id) |
| GET | `/jobs/{id}` | background job status (`model`, `env`) with log lines |
| GET | `/apps` | proxied from the server |
| GET / POST | `/repos`, `/repos/link {app, component, path}`, `/repos/auto-link` | local checkout mapping |
| POST | `/changes/analyze {app?, component?, environment, branch?}` | preflight + `ChangeReport` + values needed |
| POST / GET | `/deploys`, `/deploys/{id}` | send a deploy request / follow it |
| GET / POST | `/envs`, `/envs {app, environment, local_components, peer_requests, name?}` | list / create test env (job) |
| GET / DELETE | `/envs/{name}`, `/envs/{name}/logs?service=&tail=` | details, logs, tear down |
| GET / POST | `/peers`, `/peers {app, component, from_user, branch?, message}`, `/peers/{id}/respond?approve=` | peer sharing |
| POST | `/chat {message, session_id?}` | natural‑language interface → `{reply, steps, pending, source: llm\|rules\|confirmed}` |
| GET | `/notifications` | inbox events received from the server |

## CLI quick reference

```text
devops-agent setup --server URL --token TOKEN [--pull-model]
devops-agent link app/component PATH          devops-agent init [PATH] [--write]
devops-agent analyze [app/component] --env dev
devops-agent deploy  [app/component] --env dev [--set K=V] [--secret K=V] [--yes]
devops-agent deploy-status [ID]
devops-agent env up APP --env dev --local backend [--peer frontend=ID]
devops-agent env ls | show NAME | logs NAME [SERVICE] | down NAME [--purge]
devops-agent peer request app/component --from bob [--branch feature/x]
devops-agent peer ls | approve ID | deny ID | fetch ID
devops-agent model recommend | pull [KEY] | status
devops-agent chat ["message"]                 devops-agent daemon

devops-server serve | migrate | create-user NAME --role maintainer | check-llm
```
