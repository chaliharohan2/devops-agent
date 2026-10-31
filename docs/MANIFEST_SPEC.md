# `devops-agent.yaml` — component manifest

Each repository describes its component(s) in a `devops-agent.yaml` at the repo root. Both agents read it
(the server reads it **at the commit being deployed**). Repos without one still work: the agents *infer* a
manifest from the structure (`devops-agent init` prints the inferred draft so you can commit it).

## Minimal examples

FastAPI backend with Postgres:

```yaml
version: 1
app: shop
component:
  name: backend
  kind: backend
  language: python
  framework: fastapi
  app_module: app.main:app
  port: 8000
  dev:
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
  healthcheck: {path: /health}
  migrations: {command: alembic upgrade head}
  depends_on: [postgres]
  env:
    DATABASE_URL:
      required: true
      local: postgresql+psycopg://shop:shop@${services.postgres.host}:5432/shop
  services:
    postgres:
      image: postgres:16-alpine
      env: {POSTGRES_USER: shop, POSTGRES_PASSWORD: shop, POSTGRES_DB: shop}
      port: 5432
      healthcheck: pg_isready -U shop -d shop
```

React (Vite) or Angular frontend:

```yaml
version: 1
app: shop
component:
  name: frontend
  kind: frontend
  language: javascript        # or typescript
  framework: react            # or angular
  port: 4173                  # production server port
  dev: {command: "npm run dev -- --host 0.0.0.0 --port 5173", port: 5173}
  depends_on: [backend]
  env:
    VITE_API_URL: {required: true, local: "${components.backend.external_url}"}
```

Monorepo — list several components with their sub‑paths:

```yaml
version: 1
app: shop
components:
  - {name: api, kind: backend, language: python, framework: flask, path: services/api, port: 5000}
  - {name: web, kind: frontend, language: typescript, framework: angular, path: apps/web, port: 4200}
```

## Reference

| Field | Type | Default | Meaning |
|---|---|---|---|
| `version` | int | `1` | spec version |
| `app` | str | — | app name on the server |
| `component` / `components` | object / list | — | one component, or several (monorepo) |
| `name` | str | — | component name (unique within the app) |
| `kind` | `frontend\|backend\|worker\|database\|cache\|other` | `other` | used for defaults and UI |
| `language` | `python\|javascript\|typescript` | — | selects the inline Dockerfile template when there is no Dockerfile |
| `framework` | `fastapi\|flask\|react\|vite\|angular\|next` | — | dev command / template defaults |
| `path` | str | `.` | sub‑directory of the component in the repo |
| `port` | int | — | container port of the running service |
| `app_module` | str | — | Python ASGI/WSGI target (`app.main:app`) for templates |
| `build.dockerfile` | str | — | Dockerfile relative to `path`; if missing the agent renders an inline template |
| `build.context` / `target` / `args` | | `.` | passed to Compose `build` |
| `dev.command` | str | — | command when this is the developer's own code in a test env (hot reload) |
| `dev.mount` | str | `/app` | where the checkout is mounted in the container |
| `dev.port` | int | `port` | dev‑server port if different (e.g. Vite 5173) |
| `healthcheck.path` | str | — | HTTP path the local agent polls before saying "ready" |
| `migrations.command` | str | — | run as a one‑shot `<name>-migrate` service before the component starts |
| `depends_on` | list[str] | `[]` | other components or `services` |
| `env.<KEY>.description` | str | | shown to developers/maintainers |
| `env.<KEY>.required` | bool | `false` | warn if no value in an environment |
| `env.<KEY>.secret` | bool | `false` | treat as secret (encrypted, never sent to laptops) |
| `env.<KEY>.default` | str | | baseline value |
| `env.<KEY>.local` | str | | **test‑environment** value template (overrides server config in local envs) |
| `config.env_files` | globs | `.env.example`, `.env.sample`, `.env.template` | env templates; new keys ⇒ `env_var_added` with default |
| `config.files` | globs | inferred | other config files (YAML/JSON/INI, Angular `src/environments/*.ts`) ⇒ `config_file_changed` |
| `config.settings_modules` | globs | inferred | pydantic `BaseSettings` modules (fields are parsed with `ast`) |
| `services.<name>` | object | | infrastructure container: `image`, `env`, `port`, `healthcheck` (shell), `volumes` |
| `dependency_files` | globs | requirements*, pyproject, lockfiles, package.json… | changes ⇒ rebuild |
| `build_files` | globs | Dockerfile*, compose files, .dockerignore | changes ⇒ rebuild |
| `migration_globs` | globs | `alembic/versions/*.py`, `migrations/versions/*.py` (+ nested) | new files ⇒ migration required |

Shorthand: `env: {KEY: "default"}` or `env: [KEY, OTHER]` are accepted.

## Templates

Values in `env.*.local` (and server config values) may reference the topology of the environment being built:

| Template | Resolves to |
|---|---|
| `${components.<name>.host}` | in‑network hostname (`backend`) |
| `${components.<name>.port}` | container port |
| `${components.<name>.internal_url}` | `http://backend:8000` (container → container) |
| `${components.<name>.external_url}` | `http://localhost:18001` (browser → container) |
| `${components.<name>.host_port}` | allocated host port |
| `${services.<name>.host}` / `.port` / `.host_port` | infrastructure service address |
| `${services.<name>.env.<KEY>}` | e.g. `${services.postgres.env.POSTGRES_PASSWORD}` |

Unresolved templates are left as‑is and reported as warnings.

## How config changes are detected

`devops_common.analysis.analyze_branch()` compares `merge-base(target, branch)..branch`:

| Signal | Detection |
|---|---|
| New env var | added lines with `os.environ[...]`, `os.getenv(...)`, `environ.get(...)`, `Field(validation_alias=...)`, `config("X")`, `process.env.X`, `import.meta.env.X`, Dockerfile `ENV/ARG`; new keys in env files; new pydantic `BaseSettings` fields (AST, honours `env_prefix`) — and the name is not referenced anywhere in the component at the merge base |
| Removed env var | removed references that no longer appear anywhere in the component at head |
| Config file change | changed files matching `config.files` / `config.env_files` |
| Dependencies | changed `dependency_files` ⇒ `requires_rebuild` |
| Migrations | added files matching `migration_globs` ⇒ `requires_migration`; modified/deleted migrations ⇒ warning + high risk |
| Build | changed `build_files` ⇒ `requires_rebuild` |
| Committed secrets | AWS keys, private keys, GitHub tokens, `sk-…` keys, hard‑coded passwords ⇒ warning + high risk |

Variables moved between files (removed in one place, added in another) are not reported.
