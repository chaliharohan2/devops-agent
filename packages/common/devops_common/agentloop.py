"""A small, dependable tool-calling loop for small and mid-size open models.

Design choices that matter for SLMs running on CPU:

* tools have compact JSON schemas and short descriptions (prompt tokens are slow on CPU);
* tool results are truncated before being fed back;
* the loop is bounded (``max_steps``) and every step is recorded for the UI/audit log;
* tools marked ``mutating`` can be gated behind a confirmation callback, so the
  model can *propose* an action (e.g. "request deploy") but a human confirms it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .llm import LLMClient, LLMError


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    mutating: bool = False

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def schema_of(model_cls: type[BaseModel]) -> dict[str, Any]:
    s = model_cls.model_json_schema()
    s.pop("title", None)
    for prop in s.get("properties", {}).values():
        prop.pop("title", None)
    return s


@dataclass
class AgentStep:
    tool: str
    arguments: dict[str, Any]
    result: Any = None
    error: str | None = None
    confirmed: bool | None = None


@dataclass
class AgentRun:
    final: str
    steps: list[AgentStep] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: AgentStep | None = None


def _compact(obj: Any, limit: int) -> str:
    try:
        text = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    return text if len(text) <= limit else text[:limit] + f"... [truncated {len(text) - limit} chars]"


def run_tool_loop(
    client: LLMClient,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[Tool],
    *,
    max_steps: int = 6,
    max_tokens: int = 1024,
    result_char_limit: int = 3000,
    confirm: Callable[[AgentStep], bool] | None = None,
) -> AgentRun:
    """Run the model until it answers without tool calls (or ``max_steps``)."""
    by_name = {t.name: t for t in tools}
    convo: list[dict[str, Any]] = [{"role": "system", "content": system}, *messages]
    steps: list[AgentStep] = []
    for _ in range(max_steps):
        res = client.chat(convo, tools=[t.spec() for t in tools], max_tokens=max_tokens)
        if not res.tool_calls:
            return AgentRun(final=res.content, steps=steps, messages=convo)
        convo.append(
            {
                "role": "assistant",
                "content": res.content or "",
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in res.tool_calls
                ],
            }
        )
        for call in res.tool_calls:
            step = AgentStep(tool=call.name, arguments=call.arguments)
            steps.append(step)
            tool = by_name.get(call.name)
            if tool is None:
                step.error = f"unknown tool {call.name!r}"
            elif tool.mutating and confirm is not None and not confirm(step):
                step.confirmed = False
                return AgentRun(
                    final=f"I'd like to run `{call.name}` with {json.dumps(call.arguments)}. Please confirm.",
                    steps=steps, messages=convo, pending_confirmation=step,
                )
            else:
                step.confirmed = True if tool.mutating else None
                try:
                    step.result = tool.handler(call.arguments)
                except Exception as e:  # noqa: BLE001 - errors are fed back to the model
                    step.error = f"{type(e).__name__}: {e}"
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": _compact({"error": step.error} if step.error else step.result, result_char_limit),
                }
            )
    try:
        res = client.chat(convo + [{"role": "user", "content": "Summarize the outcome for the developer now."}],
                          max_tokens=max_tokens)
        return AgentRun(final=res.content, steps=steps, messages=convo)
    except LLMError as e:
        return AgentRun(final=f"Stopped after {max_steps} steps ({e}).", steps=steps, messages=convo)
