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

`check-llm` exits non‑zero with a specific message when the key is wrong, the model id is not served, or tool
calling is not enabled on the vLLM server.

> **Status:** the real endpoint at `103.48.50.161:8000` is **not reachable from the cloud sandbox** this project is
> developed in. Every LLM code path has been exercised end‑to‑end over real HTTP against a dummy vLLM server (see
> below), but please run `check-llm` from a machine that can reach the real endpoint before relying on it.

## Test without a GPU: the dummy vLLM server

[`devops_common/fake_vllm.py`](../packages/common/devops_common/fake_vllm.py) is a small, standard‑library‑only
server that speaks vLLM's OpenAI‑compatible API: `/v1/models`, `/v1/chat/completions`, bearer‑token auth, guided
JSON (`response_format: json_schema`), native tool calls, Qwen3 `<think>` blocks or `reasoning_content`, and
vLLM's error responses (401, unknown model, tools without `--enable-auto-tool-choice`). Replies are deterministic
and prefixed with `[fake-vllm]`. With `--log`, every request is written to a JSONL file, so you can check exactly
what the server agent sent to the model (for example, that no secret values were sent).

```bash
uv run python -m devops_common.fake_vllm --port 8001 --api-key test-key --log /tmp/vllm-requests.jsonl
export VLLM_BASE_URL=http://127.0.0.1:8001/v1 VLLM_API_KEY=test-key VLLM_MODEL=cyankiwi/Qwen3.8-27B-AWQ-INT4
uv run devops-server check-llm      # or: scripts/demo.sh, the dashboard chat, ...
```

Flags: `--reasoning-parser` (reasoning in `reasoning_content`), `--no-auto-tool-choice` (reject tool requests like
a server started without `--enable-auto-tool-choice`), `--review-risk low|medium|high` (force the review's risk, to
see the policy floor win), `--model` (served model id). The server test suite runs the same server over HTTP in
CI (`server/tests/test_pipeline.py`, tests named `*_over_http`).

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

* `--enable-auto-tool-choice` is **required** for the operator chat (and the `check-llm` tool test): without it,
  vLLM rejects any request that includes tools with HTTP 400 (`"auto" tool choice requires
  --enable-auto-tool-choice and --tool-call-parser to be set`). Reviews, conflict explanations and deployment
  diagnoses don't send tools, so they still work. If a model writes tool calls as `<tool_call>{…}</tool_call>`
  text instead of using the parser, `devops_common.llm` parses that format too.
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
