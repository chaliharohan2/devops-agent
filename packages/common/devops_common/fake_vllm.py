"""A dummy vLLM server for end-to-end testing without a GPU (standard library only).

It speaks the part of vLLM's OpenAI-compatible API that DevOps Agent uses, and
behaves like vLLM where that matters to the client:

* ``GET /health``, ``GET /v1/models``, ``POST /v1/chat/completions``;
* bearer-token auth on ``/v1/*`` when an API key is set (401 otherwise) and a 404
  error object for an unknown model;
* structured output via ``response_format: {type: json_schema}`` (guided decoding:
  the reply is bare JSON that matches the schema);
* native tool calls, as with ``vllm serve --enable-auto-tool-choice``. With
  ``auto_tool_choice=False`` a request that sends tools gets vLLM's 400 instead;
* Qwen3 "thinking": a ``<think>`` block unless ``chat_template_kwargs.enable_thinking``
  is false. With ``reasoning_parser=True`` (``--reasoning-parser qwen3``) the
  reasoning goes to ``reasoning_content`` instead of the content;
* strict request validation (roles, ``tool_call_id`` pairing, JSON-string tool
  arguments, tool schemas), so client bugs that a real server would reject fail
  here too.

Replies are deterministic and derived from the request, so tests can assert on
them. Every request is kept in :attr:`FakeVLLM.requests` (and optionally appended
to a JSONL log) so a test can check what the app sent to the model.

Run it as a standalone server::

    uv run python -m devops_common.fake_vllm --port 8001 --api-key test-key
    VLLM_BASE_URL=http://127.0.0.1:8001/v1 VLLM_API_KEY=test-key \\
        VLLM_MODEL=cyankiwi/Qwen3.8-27B-AWQ-INT4 uv run devops-server check-llm
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "cyankiwi/Qwen3.8-27B-AWQ-INT4"

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "my", "me", "i", "it", "do",
         "does", "what", "which", "with", "please", "can", "you", "use", "tool", "tools", "have", "has", "any",
         "show", "tell", "about", "this", "that", "from", "be", "there"}
_ENV_WORDS = ("dev", "development", "staging", "stage", "qa", "test", "uat", "prod", "production")


def _stems(text: str) -> list[str]:
    return [w[:5] for w in _WORD.findall(text.lower()) if w not in _STOP]


def _approx_tokens(obj: Any) -> int:
    return max(1, len(json.dumps(obj, default=str)) // 4)


class FakeVLLM:
    """Request handling, independent of the HTTP layer."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        *,
        reasoning_parser: bool = False,
        auto_tool_choice: bool = True,
        review_risk: str | None = None,
        max_model_len: int = 32768,
        log_path: Path | str | None = None,
    ):
        self.model = model
        self.api_key = api_key
        self.reasoning_parser = reasoning_parser
        self.auto_tool_choice = auto_tool_choice
        self.review_risk = review_risk
        self.max_model_len = max_model_len
        self.log_path = Path(log_path) if log_path else None
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ routing
    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, Any]:
        path = path.split("?", 1)[0].rstrip("/") or "/"
        record: dict[str, Any] = {"ts": time.time(), "method": method, "path": path}
        status, payload = self._route(method, path, {k.lower(): v for k, v in headers.items()}, body, record)
        record["status"] = status
        with self._lock:
            self.requests.append(record)
            if self.log_path:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
        return status, payload

    def chat_requests(self) -> list[dict[str, Any]]:
        """Recorded ``/v1/chat/completions`` requests that were answered successfully."""
        return [r for r in self.requests if r["path"] == "/v1/chat/completions" and r["status"] == 200]

    def _route(self, method: str, path: str, headers: dict[str, str], body: bytes,
               record: dict[str, Any]) -> tuple[int, Any]:
        if path in ("/health", "/ping") and method == "GET":
            return 200, {}
        if path == "/version" and method == "GET":
            return 200, {"version": "0.0.0-fake"}
        if not path.startswith("/v1/"):
            return 404, {"detail": "Not Found"}
        if self.api_key and headers.get("authorization") != f"Bearer {self.api_key}":
            return 401, {"error": "Unauthorized"}  # vLLM's exact response
        if path == "/v1/models" and method == "GET":
            return 200, {"object": "list", "data": [{
                "id": self.model, "object": "model", "created": int(time.time()), "owned_by": "vllm",
                "root": self.model, "parent": None, "max_model_len": self.max_model_len}]}
        if path == "/v1/chat/completions" and method == "POST":
            try:
                req = json.loads(body or b"{}")
            except json.JSONDecodeError as e:
                return _error(400, f"invalid JSON body: {e}")
            if not isinstance(req, dict):
                return _error(400, "request body must be a JSON object")
            record["request"] = req
            return self._chat(req, record)
        return 404, {"detail": "Not Found"}

    # --------------------------------------------------------------- completion
    def _chat(self, req: dict[str, Any], record: dict[str, Any]) -> tuple[int, Any]:
        if req.get("model") != self.model:
            return _error(404, f"The model `{req.get('model')}` does not exist.", "NotFoundError")
        if req.get("stream"):
            return _error(400, "this fake server does not implement streaming")
        problem = _validate_messages(req.get("messages"))
        if problem:
            return _error(400, problem)
        messages: list[dict[str, Any]] = req["messages"]
        tools = req.get("tools") or []
        problem = _validate_tools(tools)
        if problem:
            return _error(400, problem)
        tool_choice = req.get("tool_choice") or ("auto" if tools else "none")
        if tools and tool_choice == "auto" and not self.auto_tool_choice:
            return _error(400, '"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set')
        thinking = (req.get("chat_template_kwargs") or {}).get("enable_thinking", True) is not False
        fmt = req.get("response_format") or {}

        tool_calls: list[dict[str, Any]] = []
        if fmt.get("type") == "json_schema":
            schema = (fmt.get("json_schema") or {}).get("schema") or {}
            content, kind = json.dumps(self._json_for(schema, messages)), "json_schema"
        elif tools and tool_choice != "none" and messages[-1]["role"] == "user":
            call = _choose_tool(_text(messages[-1]), tools)
            if call is not None:
                tool_calls, content, kind = [call], "", "tool_call"
            else:
                content, kind = self._text_reply(messages), "text"
        elif tools and messages[-1]["role"] == "tool":
            content, kind = _answer_from_tool_results(messages), "tool_answer"
        else:
            content, kind = self._text_reply(messages), "text"

        record.update(kind=kind, thinking=thinking, tool_calls=[c["function"]["name"] for c in tool_calls])
        message: dict[str, Any] = {"role": "assistant", "content": content, "tool_calls": tool_calls}
        if thinking:
            reasoning = f"The user asked: {_text(messages[-1])[:120]!r}. I will answer from the data given."
            if self.reasoning_parser:
                message["reasoning_content"] = reasoning
            else:
                message["content"] = f"<think>\n{reasoning}\n</think>\n\n{content}"
        completion_tokens = _approx_tokens(message)
        return 200, {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "choices": [{"index": 0, "message": message, "logprobs": None,
                         "finish_reason": "tool_calls" if tool_calls else "stop", "stop_reason": None}],
            "usage": {"prompt_tokens": _approx_tokens(messages), "completion_tokens": completion_tokens,
                      "total_tokens": _approx_tokens(messages) + completion_tokens},
        }

    # ---------------------------------------------------------------- content
    def _json_for(self, schema: dict[str, Any], messages: list[dict[str, Any]]) -> Any:
        props = schema.get("properties") or {}
        if {"risk_level", "deployment_steps"} <= set(props):
            return self._review(messages)
        return _instance(schema, schema)

    def _review(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """A plausible change review derived from the facts the server agent sends."""
        try:
            facts = json.loads(_text(messages[-1]))
        except (json.JSONDecodeError, TypeError):
            facts = {}
        facts = facts if isinstance(facts, dict) else {}
        env = str(facts.get("environment") or "the target environment")
        component = facts.get("component") or "the component"
        changes = [c for c in facts.get("config_changes") or []
                   if isinstance(c, dict) and str(c.get("kind", "")).startswith("env_var")]
        keys = [str(c["key"]) for c in changes if c.get("key")]
        secrets = [str(c["key"]) for c in changes if c.get("key") and c.get("secret")]
        missing = [str(k) for k in facts.get("config_missing_in_environment") or []]
        migration = bool(facts.get("requires_migration"))
        risk = self.review_risk or ("high" if env.startswith("prod") or facts.get("analysis_discrepancies")
                                    else "medium" if (migration or keys) else "low")
        concerns = [f"Secret {k} must be provided out of band and never committed." for k in secrets]
        if migration:
            concerns.append("Includes a database migration; confirm it is backwards compatible before rollout.")
        steps = [f"Set {k} in '{env}'" for k in missing]
        steps += [f"Merge {facts.get('source_branch', 'the branch')} into {facts.get('target_branch', 'the target')}"]
        if facts.get("requires_rebuild"):
            steps.append(f"Rebuild the {component} image")
        if migration:
            steps.append("Run database migrations")
        steps.append(f"Deploy {component} to '{env}' and run the health checks")
        files = facts.get("files_changed") or []
        summary = (f"[fake-vllm] {component} change for {env}: {len(files)} file(s) changed"
                   + (f", config keys {', '.join(keys)}" if keys else "")
                   + (", with a database migration" if migration else "") + ".")
        return {"risk_level": risk, "summary": summary, "concerns": concerns, "missing_config": missing,
                "deployment_steps": steps}

    def _text_reply(self, messages: list[dict[str, Any]]) -> str:
        last = _text(messages[-1])
        low = last.lower()
        if "pong" in low:
            return "pong"
        if "json schema:" in _text(messages[0]).lower():
            # chat_json without guided decoding: models wrap JSON in prose and fences
            schema_text = _text(messages[0]).split("JSON schema:", 1)[1].strip()
            try:
                schema = json.loads(schema_text)
            except json.JSONDecodeError:
                schema = {}
            return "Here is the JSON:\n```json\n" + json.dumps(self._json_for(schema, messages)) + "\n```"
        if "deployment failed" in low:
            lines = [ln.strip() for ln in last.splitlines() if ln.strip() and not ln.startswith("```")]
            culprit = next((ln for ln in lines if re.search(r"error|fatal|failed|exception|denied|not found", ln, re.I)
                            and "deployment failed" not in ln.lower()), lines[-1] if lines else "unknown")
            return (f"[fake-vllm] Most likely root cause: `{culprit[:200]}`.\n"
                    "Next steps:\n1. Reproduce the failing step locally.\n2. Fix it on your branch, push, "
                    "and request the deploy again.")
        m = re.search(r"Merging (\S+) into (\S+) conflicts in: (.+?)\. ", last)
        if m:
            src, target, files = m.groups()
            return (f"[fake-vllm] Your branch `{src}` conflicts with `{target}` in {files}. To fix it:\n"
                    f"    git fetch origin\n    git checkout {src}\n    git merge origin/{target}\n"
                    f"    # resolve the conflicts, then\n    git commit\n    git push\n"
                    "Then request the deploy again.")
        if "summarize the outcome" in low:
            return _answer_from_tool_results(messages)
        return f"[fake-vllm] You said: {last[:200]}"


# --------------------------------------------------------------------- helpers
def _error(status: int, message: str, typ: str = "BadRequestError") -> tuple[int, Any]:
    return status, {"object": "error", "message": message, "type": typ, "param": None, "code": status}


def _text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return content or ""


def _validate_messages(messages: Any) -> str | None:
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty list"
    tool_ids: set[str] = set()
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            return f"messages[{i}] must be an object"
        role = m.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            return f"messages[{i}]: invalid role {role!r}"
        content = m.get("content")
        if content is not None and not isinstance(content, str | list):
            return f"messages[{i}]: content must be a string or a list of parts"
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if not isinstance(fn, dict) or not tc.get("id") or not fn.get("name"):
                    return f"messages[{i}]: tool_calls need an id and function.name"
                if not isinstance(fn.get("arguments"), str):
                    return f"messages[{i}]: function.arguments must be a JSON-encoded string"
                try:
                    json.loads(fn["arguments"])
                except json.JSONDecodeError:
                    return f"messages[{i}]: function.arguments is not valid JSON"
                tool_ids.add(tc["id"])
        elif role == "tool":
            if m.get("tool_call_id") not in tool_ids:
                return f"messages[{i}]: tool message references an unknown tool_call_id"
            if not isinstance(content, str):
                return f"messages[{i}]: tool content must be a string"
        elif not isinstance(content, str | list):
            return f"messages[{i}]: {role} message needs content"
    return None


def _validate_tools(tools: Any) -> str | None:
    if not isinstance(tools, list):
        return "tools must be a list"
    for i, t in enumerate(tools):
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(t, dict) or t.get("type") != "function" or not isinstance(fn, dict) or not fn.get("name"):
            return f"tools[{i}] must be {{type: function, function: {{name, ...}}}}"
        params = fn.get("parameters", {"type": "object"})
        if not isinstance(params, dict) or params.get("type") != "object":
            return f"tools[{i}].function.parameters must be a JSON schema of type object"
    return None


def _choose_tool(text: str, tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick a tool like a small model would: explicit mention first, then word overlap."""
    low = text.lower()
    fns = [t["function"] for t in tools]
    chosen = next((fn for fn in fns if fn["name"].lower() in low), None)
    if chosen is None:
        words = _stems(text)
        best: tuple[int, int] = (0, 0)
        for fn in fns:
            name_stems = set(_stems(fn["name"].replace("_", " ")))
            desc_stems = set(_stems(fn.get("description", "")))
            score = sum(2 * (w in name_stems) + (w in desc_stems) for w in set(words))
            if score == 0:
                continue
            first = min(words.index(w) for w in set(words) if w in name_stems | desc_stems)
            key = (score, -first)  # ties go to the tool matching the earliest word (usually the verb)
            if chosen is None or key > best:
                chosen, best = fn, key
    if chosen is None:
        return None
    args = _arguments(chosen.get("parameters") or {}, text)
    return {"id": f"chatcmpl-tool-{uuid.uuid4().hex[:24]}", "type": "function",
            "function": {"name": chosen["name"], "arguments": json.dumps(args)}}


def _arguments(params: dict[str, Any], text: str) -> dict[str, Any]:
    props: dict[str, Any] = params.get("properties") or {}
    required = set(params.get("required") or [])
    ints = [int(x) for x in re.findall(r"(?<![\w.])-?\d+(?![\w.])", text)]
    low = text.lower()
    env = next((w for w in _WORD.findall(low) if w in _ENV_WORDS), None)
    args: dict[str, Any] = {}
    for name, spec in props.items():
        typ = spec.get("type") if isinstance(spec, dict) else None
        explicit = re.search(rf"\b{re.escape(name)}\s*[=:]?\s+[\"']?([\w./-]+)", text, re.I)
        if typ == "integer":
            if ints:
                args[name] = ints.pop(0)
            elif name in required:
                args[name] = 1
        elif typ == "string":
            if name in ("environment", "env") and env:
                args[name] = env
            elif explicit and explicit.group(1).lower() not in _STOP:
                args[name] = explicit.group(1)
            elif name == "status" and "fail" in low:
                args[name] = "failed"
            elif name in required:
                args[name] = env or "unknown"
    return args


def _answer_from_tool_results(messages: list[dict[str, Any]]) -> str:
    results: list[tuple[str, Any]] = []
    for m in reversed(messages):
        if m["role"] == "user":
            break
        if m["role"] == "tool":
            try:
                data = json.loads(_text(m))
            except json.JSONDecodeError:
                data = _text(m)
            results.append((m.get("name") or "tool", data))
    if not results:
        return "[fake-vllm] I have no tool results to summarise."
    lines = []
    for name, data in reversed(results):
        if isinstance(data, int | float):
            lines.append(f"The result is {data}.")
        elif isinstance(data, list):
            preview = "; ".join(json.dumps(x, default=str)[:160] for x in data[:3])
            lines.append(f"{name} returned {len(data)} item(s){': ' + preview if preview else ''}")
        elif isinstance(data, dict):
            lines.append(f"{name}: " + ", ".join(f"{k}={json.dumps(v, default=str)[:80]}" for k, v in list(data.items())[:6]))
        else:
            lines.append(f"{name}: {str(data)[:300]}")
    return "[fake-vllm] " + "\n".join(lines)


def _instance(schema: dict[str, Any], root: dict[str, Any]) -> Any:
    """A minimal value that validates against ``schema`` (enough for pydantic models)."""
    if "$ref" in schema:
        node: Any = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node.get(part, {})
        return _instance(node, root)
    for key in ("anyOf", "oneOf", "allOf"):
        if schema.get(key):
            return _instance(schema[key][0], root)
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "null")
    if typ == "object" or "properties" in schema:
        return {k: _instance(v, root) for k, v in (schema.get("properties") or {}).items()}
    return {"array": [], "string": "fake", "integer": 0, "number": 0, "boolean": False}.get(typ)


# ------------------------------------------------------------------ HTTP layer
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, payload = self.server.fake.handle(self.command, self.path, dict(self.headers.items()), body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        if self.server.verbose:
            super().log_message(format, *args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], fake: FakeVLLM, verbose: bool = False):
        super().__init__(addr, _Handler)
        self.fake = fake
        self.verbose = verbose


def start(fake: FakeVLLM, host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Serve ``fake`` from a background thread; returns (server, base_url ending in /v1)."""
    httpd = _Server((host, port), fake)
    threading.Thread(target=httpd.serve_forever, name="fake-vllm", daemon=True).start()
    return httpd, f"http://{host}:{httpd.server_address[1]}/v1"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Dummy vLLM (OpenAI-compatible) server for DevOps Agent testing")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--model", default=DEFAULT_MODEL, help="served model id (VLLM_MODEL must match)")
    p.add_argument("--api-key", default=None, help="require 'Authorization: Bearer <key>' on /v1/*")
    p.add_argument("--reasoning-parser", action="store_true",
                   help="return reasoning in reasoning_content (like --reasoning-parser qwen3)")
    p.add_argument("--no-auto-tool-choice", action="store_true",
                   help="reject tool requests like a server started without --enable-auto-tool-choice")
    p.add_argument("--review-risk", choices=["low", "medium", "high"], help="force the risk level of reviews")
    p.add_argument("--log", type=Path, help="append every request (JSON lines) to this file")
    p.add_argument("--verbose", action="store_true", help="log each HTTP request to stderr")
    a = p.parse_args(argv)
    fake = FakeVLLM(a.model, a.api_key, reasoning_parser=a.reasoning_parser,
                    auto_tool_choice=not a.no_auto_tool_choice, review_risk=a.review_risk, log_path=a.log)
    httpd = _Server((a.host, a.port), fake, verbose=a.verbose)
    print(f"fake vLLM serving {a.model!r} on http://{a.host}:{httpd.server_address[1]}/v1"
          f" (api key {'required' if a.api_key else 'not required'})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
