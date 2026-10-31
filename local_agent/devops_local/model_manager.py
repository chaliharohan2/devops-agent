"""Manage the local model through Ollama (default runtime).

Ollama gives developers a one-line install on macOS/Linux/Windows, runs GGUF
models on CPU via llama.cpp, exposes an OpenAI-compatible API on
``127.0.0.1:11434/v1`` and handles model storage under ``~/.ollama``.
Any other OpenAI-compatible server (llama.cpp ``llama-server``, LM Studio) also
works — point ``llm.base_url``/``llm.model`` at it in ``config.yaml``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from devops_common.llm import LLMClient

from .config import LocalConfig
from .hardware import ModelOption

ALIAS = "devops-agent-slm"


def _ollama_root(cfg: LocalConfig) -> str:
    base = cfg.llm.base_url.rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def status(cfg: LocalConfig) -> dict[str, Any]:
    root = _ollama_root(cfg)
    out: dict[str, Any] = {"runtime": "ollama", "base_url": cfg.llm.base_url, "model": cfg.llm.model,
                           "source": cfg.llm.source, "enabled": cfg.llm.enabled}
    try:
        r = httpx.get(f"{root}/api/tags", timeout=3)
        r.raise_for_status()
        names = [m.get("name") or m.get("model") for m in r.json().get("models", [])]
        out.update(running=True, installed=names,
                   model_installed=any(n == cfg.llm.model or n == f"{cfg.llm.model}:latest" for n in names))
    except (httpx.HTTPError, ValueError) as e:
        # Maybe a non-Ollama OpenAI-compatible server
        h = client(cfg).health(timeout=3)
        out.update(running=bool(h.get("ok")), installed=h.get("models", []), model_installed=h.get("model_available"),
                   error=None if h.get("ok") else f"{type(e).__name__}: {e}")
    return out


def pull(cfg: LocalConfig, option: ModelOption, progress: Callable[[str, float | None], None] | None = None) -> None:
    """Pull ``option.source`` and create the tuned alias ``devops-agent-slm``."""
    root = _ollama_root(cfg)
    with httpx.stream("POST", f"{root}/api/pull", json={"model": option.source, "stream": True}, timeout=None) as r:
        r.raise_for_status()
        for evt in _ndjson(r.iter_lines()):
            if "error" in evt:
                raise RuntimeError(evt["error"])
            if progress:
                total, done = evt.get("total"), evt.get("completed")
                progress(evt.get("status", ""), (done / total) if total and done else None)
    # A named alias with the context window and sampling we want.
    body = {"model": ALIAS, "from": option.source,
            "parameters": {"num_ctx": option.context_tokens, "temperature": 0.2, "top_p": 0.95}}
    r = httpx.post(f"{root}/api/create", json={**body, "stream": False}, timeout=600)
    r.raise_for_status()
    cfg.llm.model = ALIAS
    cfg.llm.source = option.source
    cfg.llm.context_tokens = option.context_tokens


def _ndjson(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    for line in lines:
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def client(cfg: LocalConfig) -> LLMClient:
    return LLMClient(cfg.llm.base_url, cfg.llm.model, cfg.llm.api_key, timeout=cfg.llm.timeout_s)
