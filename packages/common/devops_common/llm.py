"""Minimal OpenAI-compatible chat client.

Works against:

* **vLLM** (server agent) — ``VLLM_BASE_URL`` may be given either as the API root
  (``http://host:8000/v1``) or the full endpoint (``.../v1/chat/completions``).
* **Ollama** / **llama.cpp server** / **LM Studio** (local agent) — all expose
  ``/v1/chat/completions``.

It deliberately avoids the ``openai`` SDK to keep the dependency surface small and
to handle the quirks of open models in one place: ``<think>`` blocks, tool calls
emitted as text (``<tool_call>{...}</tool_call>``), and JSON wrapped in fences.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_TOOL_CALL_TEXT = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0


def normalize_base_url(url: str) -> str:
    u = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    if not re.search(r"/v\d+$", u):
        u = u + "/v1"
    return u


def strip_thinking(text: str | None) -> tuple[str, str | None]:
    """Split model output into (answer, reasoning)."""
    if not text:
        return "", None
    reasoning = None
    if "</think>" in text:
        head, _, tail = text.rpartition("</think>")
        reasoning = head.replace("<think>", "").strip() or None
        text = tail
    text = _THINK_BLOCK.sub("", text)
    return text.strip(), reasoning


def extract_json(text: str) -> Any:
    """Parse the first JSON object/array in ``text`` (tolerates fences and prose)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object found in model output")


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        *,
        timeout: float = 120.0,
        no_think_params: dict[str, Any] | None = None,
        default_params: dict[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.api_key = api_key
        self.no_think_params = no_think_params or {}
        self.default_params = default_params or {}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(timeout=timeout, headers=headers, transport=transport)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ #
    def health(self, timeout: float = 10.0) -> dict[str, Any]:
        try:
            r = self._http.get(f"{self.base_url}/models", timeout=timeout)
            r.raise_for_status()
            models = [m.get("id") for m in r.json().get("data", [])]
            return {"ok": True, "base_url": self.base_url, "models": models, "configured_model": self.model,
                    "model_available": self.model in models if models else None}
        except Exception as e:  # noqa: BLE001 - surfaced to callers as data
            return {"ok": False, "base_url": self.base_url, "error": f"{type(e).__name__}: {e}"}

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_schema: dict[str, Any] | None = None,
        think: bool = False,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **self.default_params,
        }
        if not think:
            body.update(self.no_think_params)
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": json_schema.get("title", "result"), "schema": json_schema},
            }
        started = time.monotonic()
        try:
            r = self._http.post(f"{self.base_url}/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"LLM request failed: {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            # Some servers reject response_format/tool params; retry once without them.
            if json_schema is not None and r.status_code in (400, 422):
                body.pop("response_format", None)
                r = self._http.post(f"{self.base_url}/chat/completions", json=body)
            if r.status_code >= 400:
                raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected LLM response: {str(data)[:500]}") from e
        content, reasoning = strip_thinking(msg.get("content") or "")
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or reasoning
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            calls.append(ToolCall(id=tc.get("id") or uuid.uuid4().hex[:12], name=fn.get("name", ""),
                                  arguments=_parse_args(fn.get("arguments"))))
        if not calls and tools:
            for m in _TOOL_CALL_TEXT.finditer(content):
                try:
                    obj = json.loads(m.group(1))
                    calls.append(ToolCall(id=uuid.uuid4().hex[:12], name=obj.get("name", ""),
                                          arguments=_parse_args(obj.get("arguments"))))
                except json.JSONDecodeError:
                    continue
            if calls:
                content = _TOOL_CALL_TEXT.sub("", content).strip()
        return ChatResult(content=content, tool_calls=calls, reasoning=reasoning, raw=data,
                          latency_s=time.monotonic() - started)

    def chat_json(
        self, system: str, user: str, model_cls: type[T], *, max_tokens: int = 1500, retries: int = 1
    ) -> T:
        """Ask for a JSON object matching ``model_cls``; validate and repair once."""
        schema = model_cls.model_json_schema()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system + "\n\nRespond with a single JSON object only. JSON schema:\n"
             + json.dumps(schema)},
            {"role": "user", "content": user},
        ]
        last_err: Exception | None = None
        for _ in range(retries + 1):
            res = self.chat(messages, temperature=0.1, max_tokens=max_tokens, json_schema=schema)
            try:
                return model_cls.model_validate(extract_json(res.content))
            except (ValueError, ValidationError) as e:
                last_err = e
                messages += [
                    {"role": "assistant", "content": res.content[:4000]},
                    {"role": "user", "content": f"That was not valid JSON for the schema ({e}). Reply with JSON only."},
                ]
        raise LLMError(f"model did not return valid JSON: {last_err}")


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            val = json.loads(raw)
            return val if isinstance(val, dict) else {"value": val}
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}
