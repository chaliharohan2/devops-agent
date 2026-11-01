"""``devops-server`` admin CLI."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from sqlalchemy import select

app = typer.Typer(help="DevOps Agent server administration", no_args_is_help=True)


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8080, reload: bool = False) -> None:
    """Run the server agent API."""
    import uvicorn

    uvicorn.run("devops_server.main:app", host=host, port=port, reload=reload)


@app.command()
def migrate(revision: str = "head") -> None:
    """Apply database migrations (Alembic)."""
    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "alembic"))
    command.upgrade(cfg, revision)
    typer.echo(f"database migrated to {revision}")


@app.command("create-user")
def create_user(username: str, role: str = typer.Option("developer", help="developer | maintainer | admin"),
                display_name: str = "") -> None:
    """Create a user and print an API token (shown once)."""
    from .api.routes_admin import issue_token
    from .db import session_scope
    from .models import User

    if role not in ("developer", "maintainer", "admin"):
        raise typer.BadParameter("role must be developer, maintainer or admin")
    with session_scope() as s:
        if s.scalar(select(User).where(User.username == username)):
            raise typer.BadParameter(f"user {username} exists")
        u = User(username=username, display_name=display_name or username, role=role)
        s.add(u)
        s.flush()
        token = issue_token(s, u, name="cli")
    typer.echo(f"created {role} {username}\ntoken: {token}")


@app.command("check-llm")
def check_llm() -> None:
    """Verify the vLLM endpoint: list models, chat, structured JSON and tool calling."""
    from devops_common.agentloop import Tool, run_tool_loop
    from devops_common.llm import LLMError
    from devops_common.protocol import ReviewResult

    from .services import llm_service
    from .settings import get_settings

    s = get_settings()
    typer.echo(f"VLLM_BASE_URL = {s.vllm_base_url}\nVLLM_MODEL    = {s.vllm_model}\n"
               f"VLLM_API_KEY  = {'set' if s.vllm_api_key else 'NOT SET'}")
    info = llm_service.health(probe=True)
    typer.echo("health: " + json.dumps(info, indent=2))
    client = llm_service.get_client()
    if client is None or not info.get("ok"):
        raise typer.Exit(1)
    if info.get("model_available") is False:
        typer.echo(f"FAIL: VLLM_MODEL {s.vllm_model!r} is not served by this endpoint; set it to one of "
                   f"{info.get('models')}", err=True)
        raise typer.Exit(1)
    t = time.monotonic()
    try:
        review = client.chat_json(
            llm_service.SYSTEM_PROMPT + "\n" + llm_service.REVIEW_PROMPT,
            json.dumps({"app": "shop", "component": "backend", "environment": "staging",
                        "config_changes": [{"kind": "env_var_added", "key": "PAYMENT_API_KEY", "secret": True}],
                        "requires_migration": True}),
            ReviewResult,
        )
    except LLMError as e:
        typer.echo(f"FAIL: structured review: {e}", err=True)
        raise typer.Exit(1) from e
    typer.echo(f"structured review ({time.monotonic() - t:.1f}s): {review.model_dump_json(indent=2)}")
    try:
        run = run_tool_loop(
            client, "You are a test agent. Use tools when helpful.",
            [{"role": "user", "content": "What is 17 * 23? Use the multiply tool."}],
            [Tool("multiply", "Multiply two integers", {"type": "object", "properties": {"a": {"type": "integer"},
                  "b": {"type": "integer"}}, "required": ["a", "b"]}, lambda a: int(a["a"]) * int(a["b"]))],
            max_steps=3,
        )
    except LLMError as e:
        typer.echo(f"FAIL: tool calling: {e}", err=True)
        if "auto-tool-choice" in str(e):
            typer.echo("The operator chat needs native tool calls: start vLLM with "
                       "--enable-auto-tool-choice --tool-call-parser <parser>.", err=True)
        raise typer.Exit(1) from e
    used = [st.tool for st in run.steps]
    typer.echo(f"tool calling: tools used={used} final={run.final[:200]!r}")
    typer.echo("OK" if "multiply" in used else "WARNING: model did not call the tool "
               "(start vLLM with --enable-auto-tool-choice --tool-call-parser <parser>)")


if __name__ == "__main__":
    app()
