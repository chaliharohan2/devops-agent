from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import BaseModel

from devops_common.agentloop import Tool, run_tool_loop
from devops_common.compose import (
    PLACEHOLDER_SECRET,
    RenderOptions,
    SourcePlan,
    render_compose,
    services_from_manifests,
)
from devops_common.llm import LLMClient, extract_json, normalize_base_url, strip_thinking
from devops_common.manifest import infer_manifest, parse_manifest
from devops_common.protocol import ComponentRuntimeSpec, EnvironmentSpec

FRONTEND_MANIFEST = """
version: 1
app: shop
component:
  name: frontend
  kind: frontend
  language: typescript
  framework: react
  port: 80
  dev: {command: "npm run dev -- --host 0.0.0.0 --port 5173", port: 5173}
  depends_on: [backend]
  env:
    VITE_API_URL: {local: "${components.backend.external_url}"}
"""


def _spec(tmp_path: Path, backend_files: dict[str, str]):
    be = parse_manifest(backend_files["devops-agent.yaml"]).get("backend")
    fe = parse_manifest(FRONTEND_MANIFEST).get("frontend")
    spec = EnvironmentSpec(
        app="shop",
        environment="dev",
        components=[
            ComponentRuntimeSpec(name="backend", kind="backend", ref="main", manifest=be.model_dump(mode="json"),
                                 env={"LOG_LEVEL": "debug"}, secret_keys=["PAYMENT_API_KEY"]),
            ComponentRuntimeSpec(name="frontend", kind="frontend", ref="main", manifest=fe.model_dump(mode="json")),
        ],
        services=services_from_manifests([be, fe]),
    )
    (tmp_path / "be").mkdir()
    (tmp_path / "fe").mkdir()
    sources = {
        "backend": SourcePlan("backend", tmp_path / "be", mode="dev", origin="local"),
        "frontend": SourcePlan("frontend", tmp_path / "fe", mode="build", origin="clone"),
    }
    return spec, sources


def test_render_test_environment(tmp_path: Path, backend_files: dict[str, str]) -> None:
    spec, sources = _spec(tmp_path, backend_files)
    res = render_compose(spec, sources, RenderOptions(project="dxa-shop", host_ports={"backend": 18000, "frontend": 13000, "postgres": 15432}))
    svc = res.document["services"]
    assert set(svc) == {"postgres", "backend", "backend-migrate", "frontend"}
    be = svc["backend"]
    assert be["environment"]["DATABASE_URL"] == "postgresql+psycopg://app:app@postgres:5432/app"
    assert be["environment"]["PAYMENT_API_KEY"] == PLACEHOLDER_SECRET
    assert be["environment"]["LOG_LEVEL"] == "debug"
    assert be["volumes"][0].endswith(":/app")
    assert be["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "backend-migrate": {"condition": "service_completed_successfully"},
    }
    assert "dockerfile_inline" in be["build"]  # no Dockerfile in repo -> template
    fe = svc["frontend"]
    assert fe["environment"]["VITE_API_URL"] == "http://localhost:18000"
    assert fe["ports"] == ["127.0.0.1:13000:5173"]
    assert res.stubbed_secrets == {"backend": ["PAYMENT_API_KEY"]}
    assert res.urls["frontend"] == "http://localhost:13000"
    assert "postgres" in res.yaml()


def test_render_deploy_has_no_local_overrides(tmp_path: Path, backend_files: dict[str, str]) -> None:
    spec, sources = _spec(tmp_path, backend_files)
    spec.components[0].env["DATABASE_URL"] = "postgresql+psycopg://prod/db"
    sources["backend"].mode = "build"
    res = render_compose(spec, sources, RenderOptions(project="shop-prod", purpose="deploy", restart="unless-stopped",
                                                      persist_services=True, host_ports={"frontend": 8080}))
    be = res.document["services"]["backend"]
    assert be["environment"]["DATABASE_URL"] == "postgresql+psycopg://prod/db"
    assert "PAYMENT_API_KEY" not in be["environment"]  # secrets come only from the config store
    assert "volumes" not in be and be["restart"] == "unless-stopped"
    assert "postgres-data" in res.document["volumes"]


def test_infer_manifest_fastapi_and_react(tmp_path: Path) -> None:
    (tmp_path / "api" / "app").mkdir(parents=True)
    (tmp_path / "api" / "requirements.txt").write_text("fastapi\nsqlalchemy\npsycopg\n")
    (tmp_path / "api" / "app" / "main.py").write_text("app = None\n")
    (tmp_path / "api" / "alembic.ini").write_text("[alembic]\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text(json.dumps({"dependencies": {"react": "19"}, "devDependencies": {"vite": "7"}}))
    m = infer_manifest(tmp_path, app="shop")
    api, web = m.get("api"), m.get("web")
    assert api.framework == "fastapi" and api.app_module == "app.main:app" and api.migrations
    assert api.services["postgres"].image.startswith("postgres")
    assert web.kind == "frontend" and web.framework == "react" and web.port == 5173
    assert m.component_for_path("api/app/main.py").name == "api"


def test_llm_helpers() -> None:
    assert normalize_base_url("http://h:8000/v1/chat/completions") == "http://h:8000/v1"
    assert normalize_base_url("http://localhost:11434") == "http://localhost:11434/v1"
    assert strip_thinking("<think>hmm</think>\nanswer") == ("answer", "hmm")
    assert strip_thinking("reasoning only</think>final")[0] == "final"
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}


class _Out(BaseModel):
    risk: str
    n: int


def _mock_client(replies: list[dict]) -> tuple[LLMClient, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": replies[len(seen) - 1]}]})

    return LLMClient("http://llm/v1/chat/completions", "m", "k", transport=httpx.MockTransport(handler),
                     no_think_params={"chat_template_kwargs": {"enable_thinking": False}}), seen


def test_chat_json_repairs_once() -> None:
    client, seen = _mock_client([{"content": "not json"}, {"content": '<think>x</think>{"risk": "low", "n": 2}'}])
    out = client.chat_json("sys", "user", _Out)
    assert out == _Out(risk="low", n=2)
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert len(seen) == 2


def test_tool_loop_parses_text_tool_calls_and_confirms() -> None:
    client, _ = _mock_client([
        {"content": '<tool_call>{"name": "add", "arguments": {"a": 1, "b": 2}}</tool_call>'},
        {"content": "The answer is 3."},
    ])
    tools = [Tool("add", "add numbers", {"type": "object", "properties": {}}, lambda a: a["a"] + a["b"])]
    run = run_tool_loop(client, "sys", [{"role": "user", "content": "1+2?"}], tools)
    assert run.final == "The answer is 3."
    assert run.steps[0].result == 3

    client2, _ = _mock_client([{"content": "", "tool_calls": [
        {"id": "1", "function": {"name": "deploy", "arguments": '{"env": "prod"}'}}]}])
    tools2 = [Tool("deploy", "deploy", {"type": "object"}, lambda a: "done", mutating=True)]
    run2 = run_tool_loop(client2, "sys", [{"role": "user", "content": "ship it"}], tools2, confirm=lambda s: False)
    assert run2.pending_confirmation is not None and run2.pending_confirmation.arguments == {"env": "prod"}


@pytest.mark.parametrize("bad", ["", "manifest: [1"])
def test_parse_manifest_errors(bad: str) -> None:
    with pytest.raises((ValueError, yaml.YAMLError)):
        parse_manifest(bad)
