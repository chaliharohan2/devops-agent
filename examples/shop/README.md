# Example app: `shop`

A two‑repo web shop used by `scripts/demo.sh` and as a template for onboarding real services.

| Component | Stack | Manifest |
|---|---|---|
| `backend/` | Python 3.11, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, pydantic‑settings | [`backend/devops-agent.yaml`](backend/devops-agent.yaml) |
| `frontend/` | React 19 + Vite (no Dockerfile — the agents render one) | [`frontend/devops-agent.yaml`](frontend/devops-agent.yaml) |

Things the manifests demonstrate:

* `env.*.local` templates wire components together in local test environments
  (`DATABASE_URL` → the env's Postgres, `VITE_API_URL` → the backend's host URL, `CORS_ORIGINS` → the frontend URL);
* `migrations.command` becomes a one‑shot `backend-migrate` service that runs before the backend starts;
* `dev.command` runs the developer's own checkout with hot reload;
* `services.postgres` declares infrastructure once; it is shared by every component that depends on it.

In the demo the two directories are pushed to separate bare git repositories, exactly like two GitHub repos.
