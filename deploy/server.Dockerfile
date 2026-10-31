# Server agent image: API + dashboard UI + git + docker CLI (for the `compose` deploy target).
# Build from the repository root:  docker build -f deploy/server.Dockerfile -t devops-agent-server .

FROM node:22-alpine AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM docker:27-cli AS dockercli

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
# Workspace metadata first for layer caching
COPY pyproject.toml uv.lock ./
COPY packages/common/pyproject.toml packages/common/pyproject.toml
COPY server/pyproject.toml server/pyproject.toml
COPY local_agent/pyproject.toml local_agent/pyproject.toml
COPY packages/common packages/common
COPY server server
COPY local_agent local_agent
RUN uv sync --frozen --no-dev --package devops-server
COPY --from=web /src/web/dist web/dist

ENV PATH="/app/.venv/bin:$PATH" DEVOPS_DATA_DIR=/data
RUN git config --system user.name "DevOps Server Agent" \
 && git config --system user.email "devops-agent@localhost" \
 && mkdir -p /data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD curl -fsS http://127.0.0.1:8080/api/v1/health || exit 1
CMD ["sh", "-c", "devops-server migrate && devops-server serve --host 0.0.0.0 --port 8080"]
