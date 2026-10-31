#!/usr/bin/env bash
# End-to-end demo of the DevOps Agent fleet on a single machine.
#
#   scripts/demo.sh            # run the whole story
#   scripts/demo.sh down       # stop everything and delete .demo/
#
# Story:
#   1. start Postgres + the server agent
#   2. create two git remotes (shop-backend, shop-frontend) from examples/shop
#   3. register app "shop" with environments dev (auto-deploy) and prod (approval)
#   4. deploy the baseline of dev with Docker Compose
#   5. developer "alice" sets up her local agent, changes the backend (new setting,
#      new migration), commits and pushes — the agent never does that for her
#   6. her agent analyses the pushed branch and asks the server agent to deploy it;
#      the server agent re-verifies, reviews, merges, updates config and deploys
#   7. alice builds an end-to-end test env: her backend (live reload) + the dev
#      frontend + Postgres in Docker, and gets instructions on how to test it
#
# Requirements: docker (with compose v2), git, curl, jq, uv.
# Optional env: DEMO_DEPLOY_TARGET=dry_run to skip real server-side deployments.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEMO="$ROOT/.demo"
PORT="${DEMO_SERVER_PORT:-8080}"
SERVER="http://127.0.0.1:$PORT"
PG_PORT="${DEMO_PG_PORT:-55433}"
TARGET="${DEMO_DEPLOY_TARGET:-compose}"
ADMIN_TOKEN="dxa_demo_admin_token_change_me"
export DATABASE_URL="postgresql+psycopg://devops:devops@127.0.0.1:$PG_PORT/devops_agent"

step() { printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }
api() { # api METHOD PATH TOKEN [JSON]
  local method=$1 path=$2 token=$3 body=${4:-}
  if [ -n "$body" ]; then
    curl -fsS -X "$method" "$SERVER/api/v1$path" -H "Authorization: Bearer $token" -H 'Content-Type: application/json' -d "$body"
  else
    curl -fsS -X "$method" "$SERVER/api/v1$path" -H "Authorization: Bearer $token"
  fi
}
dev_git() { git -c user.name="Alice Developer" -c user.email=alice@example.com "$@"; }

down() {
  step "Stopping demo"
  for p in $(docker compose ls -a --format json 2>/dev/null | jq -r '.[].Name' | grep -E '^(dxa-shop|shop-)' || true); do
    docker compose -p "$p" down -v --remove-orphans >/dev/null 2>&1 || true
  done
  [ -f "$DEMO/server.pid" ] && kill "$(cat "$DEMO/server.pid")" 2>/dev/null || true
  docker rm -f dxa-demo-pg >/dev/null 2>&1 || true
  rm -rf "$DEMO"
  echo "done"
}

if [ "${1:-}" = "down" ]; then down; exit 0; fi
[ -d "$DEMO" ] && { echo "A previous demo exists; run 'scripts/demo.sh down' first."; exit 1; }
mkdir -p "$DEMO"
cd "$ROOT"

step "1. Installing Python workspace and building the web UI"
uv sync --all-packages -q
if [ ! -d web/dist ]; then (cd web && npm install --no-audit --no-fund -s && npm run build -s); fi

step "1. Starting Postgres for the server agent (port $PG_PORT)"
docker run -d --name dxa-demo-pg -e POSTGRES_USER=devops -e POSTGRES_PASSWORD=devops -e POSTGRES_DB=devops_agent \
  -p "127.0.0.1:$PG_PORT:5432" postgres:16-alpine >/dev/null
until docker exec dxa-demo-pg pg_isready -U devops -d devops_agent >/dev/null 2>&1; do sleep 1; done
sleep 2
uv run devops-server migrate

step "1. Starting the server agent on $SERVER (LLM: ${VLLM_BASE_URL:-not configured})"
DEVOPS_BOOTSTRAP_ADMIN_TOKEN="$ADMIN_TOKEN" DEVOPS_DATA_DIR="$DEMO/server-data" DEVOPS_DEFAULT_DEPLOY_TARGET="$TARGET" \
DEVOPS_SECRET_KEY="$(uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  nohup uv run devops-server serve --host 127.0.0.1 --port "$PORT" >"$DEMO/server.log" 2>&1 &
echo $! >"$DEMO/server.pid"
until curl -fsS "$SERVER/api/v1/health" >/dev/null 2>&1; do sleep 1; done
echo "server agent is up (log: $DEMO/server.log)"

step "2. Creating git remotes from examples/shop (stand-ins for GitHub/GitLab repos)"
for comp in backend frontend; do
  remote="$DEMO/remotes/shop-$comp.git"
  git init -q --bare -b main "$remote"
  seed="$DEMO/seed/shop-$comp"
  mkdir -p "$seed" && cp -R "examples/shop/$comp/." "$seed/"
  (cd "$seed" && git init -q -b main && git add -A && dev_git commit -qm "Initial $comp" && git remote add origin "$remote" && git push -q origin main)
  echo "  $remote"
done

step "3. Registering users and the 'shop' app"
LEAD=$(api POST /users "$ADMIN_TOKEN" '{"username":"lead","role":"maintainer","display_name":"Team Lead"}' | jq -r .token)
ALICE=$(api POST /users "$ADMIN_TOKEN" '{"username":"alice","role":"developer","display_name":"Alice"}' | jq -r .token)
echo "$LEAD" >"$DEMO/lead.token"; echo "$ALICE" >"$DEMO/alice.token"
api POST /apps "$LEAD" "$(jq -n --arg t "$TARGET" '{name:"shop", description:"Demo web shop",
  environments:[
    {name:"dev", branch:"main", auto_deploy:true, deploy_target:{type:$t, host_ports:{frontend:8091, backend:8090}}},
    {name:"prod", branch:"main", requires_approval:true, deploy_target:{type:"dry_run"}}]}')" >/dev/null
for comp in backend frontend; do
  api POST /apps/shop/components/import "$LEAD" "{\"repo_url\":\"$DEMO/remotes/shop-$comp.git\"}" | jq -r '.[] | "  component \(.name) (\(.kind)) <- \(.repo_url)"'
done
api PUT /apps/shop/environments/dev/configs "$LEAD" '{"message":"initial dev config","values":[
  {"key":"DATABASE_URL","value":"postgresql+psycopg://shop:shop@postgres:5432/shop","is_secret":true,"component":"backend"},
  {"key":"CORS_ORIGINS","value":"http://localhost:8091","component":"backend"},
  {"key":"VITE_API_URL","value":"http://localhost:8090","component":"frontend"}]}' | jq -c .

step "4. Deploying the dev baseline (target: $TARGET)"
DEP=$(api POST /apps/shop/environments/dev/deploy "$LEAD" '{}' | jq -r .id)
while :; do
  S=$(api GET "/deployments/$DEP" "$LEAD" | jq -r .status)
  [ "$S" = "succeeded" ] || [ "$S" = "failed" ] && break
  sleep 3
done
echo "deployment #$DEP: $S"
[ "$S" = "succeeded" ] || { api GET "/deployments/$DEP" "$LEAD" | jq -r .log | tail -40; exit 1; }

step "5. Alice sets up her local agent"
export DEVOPS_AGENT_HOME="$DEMO/alice"
mkdir -p "$DEMO/alice/code"
git clone -q "$DEMO/remotes/shop-backend.git" "$DEMO/alice/code/shop-backend"
git clone -q "$DEMO/remotes/shop-frontend.git" "$DEMO/alice/code/shop-frontend"
uv run devops-agent setup --server "$SERVER" --token "$ALICE"
uv run devops-agent link shop/backend "$DEMO/alice/code/shop-backend"
uv run devops-agent link shop/frontend "$DEMO/alice/code/shop-frontend"

step "5. Alice adds a discount feature to the backend, commits and pushes (by hand, as always)"
B="$DEMO/alice/code/shop-backend"
(cd "$B" && git checkout -q -b feature/discounts)
python3 - "$B" <<'PY'
import pathlib, sys
b = pathlib.Path(sys.argv[1])
s = b / "app/settings.py"
s.write_text(s.read_text().replace('    app_name: str = "Shop API"\n',
    '    app_name: str = "Shop API"\n    discount_percent: float = 0\n'))
m = b / "app/main.py"
m.write_text(m.read_text().replace(
    "return [ItemOut(id=i.id, name=i.name, price=float(i.price)) for i in db.scalars(select(Item).order_by(Item.id))]",
    "factor = 1 - settings.discount_percent / 100\n"
    "    return [ItemOut(id=i.id, name=i.name, price=round(float(i.price) * factor, 2))\n"
    "            for i in db.scalars(select(Item).order_by(Item.id))]"))
(b / "alembic/versions/0002_item_stock.py").write_text('''"""add stock"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.add_column("items", sa.Column("stock", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("items", "stock")
''')
PY
(cd "$B" && git add -A && dev_git commit -qm "Discounts via DISCOUNT_PERCENT + stock column" && git push -q -u origin feature/discounts)
echo "pushed feature/discounts"

step "6. Alice: 'what does my branch change for dev?'"
(cd "$B" && uv run --project "$ROOT" devops-agent analyze --env dev)

step "6. Alice: 'deploy it to dev' (the server agent merges, configures and deploys)"
(cd "$B" && uv run --project "$ROOT" devops-agent deploy --env dev --set DISCOUNT_PERCENT=10 --yes --notes "demo")

if [ "$TARGET" = "compose" ]; then
  echo "dev backend now serves discounted prices:"
  for i in $(seq 1 30); do curl -fsS http://localhost:8090/api/items >/dev/null 2>&1 && break; sleep 2; done
  curl -fsS http://localhost:8090/api/items | jq -c .
fi

step "7. Alice: 'set up an end-to-end test env for my backend'"
uv run devops-agent env up shop --env dev --local backend
ENV_STATE="$DEMO/alice/envs/shop-dev-backend/state.json"
BE_URL=$(jq -r .urls.backend "$ENV_STATE"); FE_URL=$(jq -r .urls.frontend "$ENV_STATE")
echo "test env backend:  $(curl -fsS "$BE_URL/health")"
echo "test env items:    $(curl -fsS "$BE_URL/api/items" | jq -c .)"
echo "test env frontend: $(curl -fsS -o /dev/null -w '%{http_code}' "$FE_URL")"

step "Done!"
cat <<EOF
  Server dashboard : $SERVER   (token for lead: $(cat "$DEMO/lead.token"))
  Developer app    : DEVOPS_AGENT_HOME=$DEMO/alice uv run devops-agent daemon   -> http://127.0.0.1:7600
  Alice's test env : frontend $FE_URL   backend $BE_URL
  Tear down        : scripts/demo.sh down
EOF
