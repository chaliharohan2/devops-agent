# Server agent LLM (vLLM)

The server agent talks to an **OpenAI‑compatible vLLM endpoint**. Configuration (environment variables):

| Variable | Example | Notes |
|---|---|---|
| `VLLM_BASE_URL` | `http://103.48.50.161:8000/v1` or `.../v1/chat/completions` | both forms accepted |
| `VLLM_API_KEY` | `…` | sent as `Authorization: Bearer` |
| `VLLM_MODEL` | `cyankiwi/Qwen3.8-27B-AWQ-INT4` | must match a model id served by vLLM |
| `DEVOPS_LLM_ENABLED` | `true` | set `false` to force deterministic mode |
| `DEVOPS_LLM_TIMEOUT_S` | `120` | read timeout; connect timeout is capped at 10 s so outages fail fast |
| `DEVOPS_LLM_NO_THINK_PARAMS` | `{"chat_template_kwargs": {"enable_thinking": false}}` | extra body for structured calls (Qwen3.x "thinking" off) |

## Verify the endpoint

```bash
uv run devops-server check-llm
```

It prints the configuration, lists `/v1/models`, sends a "pong" probe (with latency), asks for a schema‑validated
JSON review, and runs a one‑tool function‑calling round trip. The dashboard's Overview shows the same health.

> **Status at hand‑off:** the endpoint at `103.48.50.161:8000` was **not reachable from the sandbox this project
> was built in** (direct egress timed out; the sandbox proxy only allows HTTPS on port 443). All LLM code paths
> are covered with an in‑process mock (`httpx.MockTransport`) in the tests, and the demo ran with the deterministic
> fallback. Please run `check-llm` from a machine that can reach the endpoint.

### Recommended vLLM flags

```bash
vllm serve cyankiwi/Qwen3.8-27B-AWQ-INT4 \
  --api-key "$VLLM_API_KEY" \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --reasoning-parser qwen3 \
  --max-model-len 32768
```

* `--tool-call-parser`: use the parser named on the model card (`hermes` for most Qwen3.x chat templates,
  `qwen3_coder` for coder‑style templates).
* `--reasoning-parser qwen3` moves `<think>` blocks into `reasoning_content` (the client strips them either way).

* Without `--enable-auto-tool-choice`, the operator chat still works if the model emits `<tool_call>{…}</tool_call>`
  text — `devops_common.llm` parses that format — but native tool calls are more reliable.
* Structured outputs use `response_format: {type: json_schema}` (vLLM guided decoding). If the server rejects it,
  the client retries without it and validates/repairs the JSON itself.

## What the LLM does (and does not do)

| Use | Input | Output | Guardrail |
|---|---|---|---|
| **Change review** (`review_change`) | facts from the deterministic `ChangeReport`, discrepancies, missing config, policy risk floor | `ReviewResult` JSON: `risk_level`, `summary`, `concerns`, `missing_config`, `deployment_steps` | schema‑validated; final risk = `max(floor, llm)`; missing config is a union (LLM can add, not remove); never approves |
| **Conflict explanation** | branch, conflicting files, commit subjects | friendly message with git commands | text only; the server never resolves conflicts |
| **Deployment diagnosis** | last 6 KB of the deploy log | likely root cause + next steps | text only, stored on the deployment |
| **Operator chat** (`POST /agent/chat`) | user question | answer | tools are **read‑only** (list apps/CRs/deployments, config *keys*, audit); approvals stay in the UI |

Prompts live in `server/devops_server/services/llm_service.py` (`SYSTEM_PROMPT`, `REVIEW_PROMPT`). The system prompt
states the hard rules (no code changes, no self‑approval, facts only).

## Fallback behaviour

If the endpoint is unconfigured, down or returns invalid JSON twice, the server agent uses
`deterministic_review()` — the summary from the analyzer, the policy reasons as concerns, and a rule‑based rollout
plan — and records `(LLM review unavailable: …)` in the concerns. Pipelines never block on the LLM.

## Why Qwen3.8‑27B AWQ for the server

A 27B dense model at INT4 fits a single 24–48 GB GPU, is strong at structured output and tool calling, and the server
agent's tasks are short (reviews of a few KB of facts). The server is the place to spend model capacity: it reviews
every change, while laptops only need a fast routing/summarisation model.
