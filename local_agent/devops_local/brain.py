"""Natural-language front end for the local agent.

The local SLM turns requests like *"deploy my payments branch to staging"* or
*"set up an e2e env so I can test my backend"* into tool calls on
:class:`LocalAgent`. Design choices for a ~9B model on a CPU:

* few, small tools with terse schemas; results are trimmed before re-prompting;
* outward-facing actions (deploy, peer requests, tearing down) need an explicit
  "yes" from the developer — the model proposes, the human confirms;
* a deterministic intent router handles the common phrasings when the model is
  unavailable (not installed yet, still loading, too slow), so the app keeps working.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict
from typing import Any

from devops_common.agentloop import AgentStep, Tool, run_tool_loop
from devops_common.llm import LLMError
from devops_common.protocol import ConfigValue

from . import model_manager
from .service import AgentError, LocalAgent

SYSTEM = """You are the developer's local DevOps agent, running on their laptop.
You help them (1) deploy code they have ALREADY pushed, and (2) spin up Docker test environments.
Rules you must follow:
- You can never commit, push or edit code. If something is not pushed, tell the developer the exact git command to run.
- Deploys are done by the server agent: you analyse the pushed branch and send a request.
- Use tools to get facts; never invent branch names, URLs, ports or config values.
- Ask for missing config values instead of guessing them. Secrets are entered by the developer.
- Keep answers short and practical: what happened, what the developer should do next.
Known apps/components: {apps}. Linked local checkouts: {links}."""

YES = re.compile(r"^\s*(y|yes|yep|ok|okay|sure|confirm|do it|go|go ahead|proceed)\b", re.I)
NO = re.compile(r"^\s*(n|no|nope|cancel|stop|abort)\b", re.I)


class Brain:
    def __init__(self, agent: LocalAgent):
        self.agent = agent
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self.pending: dict[str, AgentStep] = {}

    # ------------------------------------------------------------------ tools
    def tools(self) -> list[Tool]:
        a = self.agent
        s = {"type": "string"}

        def obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
            return {"type": "object", "properties": props, "required": required}

        def analyze(args: dict[str, Any]) -> Any:
            app, comp = a.resolve(args.get("app"), args.get("component"))
            r = a.analyze(app, comp, args["environment"], args.get("branch"))
            rep = r.get("report") or {}
            return {"preflight_problems": r["preflight"]["problems"], "warnings": r["preflight"]["warnings"],
                    "target_branch": r["target_branch"], "summary": rep.get("summary"),
                    "config_values_needed": r.get("values_needed", []), "report_warnings": rep.get("warnings", [])}

        def deploy(args: dict[str, Any]) -> Any:
            app, comp = a.resolve(args.get("app"), args.get("component"))
            needed = {v["key"]: v for v in a.analyze(app, comp, args["environment"], args.get("branch"))
                      .get("values_needed", [])}
            values = [ConfigValue(key=k, value=str(v), is_secret=needed.get(k, {}).get("secret", False))
                      for k, v in (args.get("config") or {}).items()]
            cr = a.deploy(app, comp, args["environment"], values, args.get("branch"))
            return {"change_request": cr.id, "status": cr.status, "risk": cr.risk_level, "error": cr.error,
                    "review": cr.review.summary if cr.review else None}

        def env_up(args: dict[str, Any]) -> Any:
            app = args.get("app") or a.resolve(None, None)[0]
            local = args.get("local_components") or [a.resolve(app, None)[1]]
            st = a.env_up(app, args.get("environment") or "dev", local)
            return {"name": st.name, "status": st.status, "urls": st.urls, "message": st.message,
                    "instructions": st.instructions}

        def env_status(args: dict[str, Any]) -> Any:
            envs = a.envs.list()
            if args.get("name"):
                envs = [e for e in envs if e.name == args["name"]]
            return [{"name": e.name, "status": e.status, "urls": e.urls, "message": e.message} for e in envs]

        def env_down(args: dict[str, Any]) -> Any:
            return {"name": args["name"], "status": a.env_down(args["name"]).status}

        def deploy_status(args: dict[str, Any]) -> Any:
            return [{"id": c.id, "app": c.app, "component": c.component, "env": c.environment, "status": c.status,
                     "risk": c.risk_level, "error": (c.error or "")[:400]}
                    for c in a.deploy_status(args.get("id"))[:5]]

        def peer(args: dict[str, Any]) -> Any:
            app, comp = a.resolve(args.get("app"), args.get("component"))
            pr = a.peer_request(app, comp, args["from_user"], branch=args.get("branch"), message=args.get("message", ""))
            return {"peer_request": pr.id, "status": pr.status, "waiting_for": pr.to_user}

        return [
            Tool("analyze_changes", "Check a pushed branch: unpushed work, config changes, values needed.",
                 obj({"environment": s, "app": s, "component": s, "branch": s}, ["environment"]), analyze),
            Tool("request_deploy", "Ask the server agent to merge the pushed branch and deploy it.",
                 obj({"environment": s, "app": s, "component": s, "branch": s,
                      "config": {"type": "object", "description": "KEY: value for new config"}}, ["environment"]),
                 deploy, mutating=True),
            Tool("setup_test_env", "Start a Docker test env: local code for my components, server versions for the rest.",
                 obj({"app": s, "environment": s, "local_components": {"type": "array", "items": s}}, []), env_up),
            Tool("test_env_status", "List local test environments.", obj({"name": s}, []), env_status),
            Tool("teardown_test_env", "Stop and remove a test environment.", obj({"name": s}, ["name"]), env_down,
                 mutating=True),
            Tool("deploy_status", "Status of my deploy requests.", obj({"id": {"type": "integer"}}, []), deploy_status),
            Tool("request_peer_code", "Ask a teammate's agent for their code (branch, or working tree if no branch).",
                 obj({"from_user": s, "app": s, "component": s, "branch": s, "message": s}, ["from_user"]), peer,
                 mutating=True),
        ]

    # ------------------------------------------------------------------- chat
    def chat(self, message: str, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or uuid.uuid4().hex[:12]
        history = self.sessions.setdefault(sid, [])
        pending = self.pending.pop(sid, None)
        if pending is not None:
            if YES.match(message):
                return self._run_confirmed(sid, pending)
            if NO.match(message):
                return {"session_id": sid, "reply": "Cancelled.", "steps": [], "source": "rules"}
        history.append({"role": "user", "content": message})
        del history[:-12]
        try:
            client = model_manager.client(self.agent.cfg) if self.agent.cfg.llm.enabled else None
            if client is None:
                raise LLMError("local model disabled")
            run = run_tool_loop(client, self._system(), list(history), self.tools(), max_steps=5,
                                max_tokens=700, result_char_limit=2500, confirm=lambda step: False)
            if run.pending_confirmation:
                self.pending[sid] = run.pending_confirmation
            history.append({"role": "assistant", "content": run.final})
            return {"session_id": sid, "reply": run.final, "source": "llm",
                    "steps": [asdict(st) for st in run.steps],
                    "pending": asdict(run.pending_confirmation) if run.pending_confirmation else None}
        except (LLMError, OSError) as e:
            reply = self.rules(message, sid)
            reply["reply"] += f"\n\n(local model unavailable: {e}; used built-in commands)"
            return reply

    def _system(self) -> str:
        try:
            apps = {a["name"]: [c["name"] for c in a["components"]] for a in self.agent.server.apps()}
        except Exception:  # noqa: BLE001 - prompt context only
            apps = {}
        return SYSTEM.format(apps=apps or "unknown", links=list(self.agent.cfg.repos) or "none")

    def _run_confirmed(self, sid: str, step: AgentStep) -> dict[str, Any]:
        tool = next((t for t in self.tools() if t.name == step.tool), None)
        if tool is None:
            return {"session_id": sid, "reply": f"Unknown action {step.tool}", "steps": []}
        try:
            step.result = tool.handler(step.arguments)
            step.confirmed = True
            reply = f"Done: {step.tool}.\n{_fmt(step.result)}"
        except (AgentError, LLMError, OSError, KeyError) as e:
            step.error = str(e)
            reply = f"{step.tool} failed: {e}"
        self.sessions.setdefault(sid, []).append({"role": "assistant", "content": reply})
        return {"session_id": sid, "reply": reply, "steps": [asdict(step)], "source": "confirmed"}

    # -------------------------------------------------------- rules fallback
    def rules(self, message: str, sid: str) -> dict[str, Any]:
        msg = message.lower()
        a = self.agent
        envs = re.findall(r"\b(?:to|in|on|against)\s+([a-z0-9_-]+)\b", msg)
        env = next((e for e in envs if e not in {"the", "my", "a", "test"}), None)
        try:
            if re.search(r"\b(deploy|ship|release|merge)\b", msg):
                if not env:
                    return {"session_id": sid, "reply": "Which environment should I deploy to (e.g. dev, staging)?", "steps": []}
                app, comp = a.resolve(None, None)
                info = self.tools()[0].handler({"environment": env, "app": app, "component": comp})
                if info["preflight_problems"]:
                    return {"session_id": sid, "reply": "Cannot deploy yet:\n- " + "\n- ".join(info["preflight_problems"]),
                            "steps": [], "source": "rules"}
                step = AgentStep(tool="request_deploy", arguments={"environment": env, "app": app, "component": comp})
                self.pending[sid] = step
                needed = [v["key"] for v in info["config_values_needed"] if v["required"]]
                extra = f"\nNew config needing values: {', '.join(needed)} (set them in the Deploy tab)." if needed else ""
                return {"session_id": sid, "source": "rules", "steps": [],
                        "reply": f"{info['summary']}\nTarget: {info['target_branch']} ({env}).{extra}\n"
                                 f"Send the deploy request to the server agent? (yes/no)"}
            if re.search(r"\b(tear ?down|destroy|stop|remove)\b", msg):
                names = [e.name for e in a.envs.list() if e.name in msg] or [e.name for e in a.envs.list()][:1]
                if not names:
                    return {"session_id": sid, "reply": "No test environments are running.", "steps": []}
                self.pending[sid] = AgentStep(tool="teardown_test_env", arguments={"name": names[0]})
                return {"session_id": sid, "reply": f"Tear down {names[0]}? (yes/no)", "steps": [], "source": "rules"}
            if re.search(r"\b(test|e2e|end to end|end-to-end|environment|env|spin up|set up|setup)\b", msg) and \
                    not re.search(r"\bstatus\b", msg):
                res = self.tools()[2].handler({"environment": env or "dev"})
                return {"session_id": sid, "reply": res["instructions"] if res["status"] == "running"
                        else f"Environment {res['name']} is {res['status']}: {res['message']}", "steps": [], "source": "rules"}
            if "status" in msg or "deploy" in msg:
                return {"session_id": sid, "reply": _fmt(self.tools()[5].handler({})), "steps": [], "source": "rules"}
        except AgentError as e:
            return {"session_id": sid, "reply": str(e), "steps": [], "source": "rules"}
        return {"session_id": sid, "source": "rules", "steps": [],
                "reply": "I can: analyse & deploy your pushed branch (\"deploy to dev\"), build a test environment "
                         "(\"set up a test env\"), show status, tear environments down, or fetch a teammate's code."}


def _fmt(obj: Any) -> str:
    if isinstance(obj, dict):
        if "instructions" in obj and obj.get("instructions"):
            return str(obj["instructions"])
        return "\n".join(f"- {k}: {v}" for k, v in obj.items())
    if isinstance(obj, list):
        return "\n".join(_fmt(x) if not isinstance(x, dict) else ", ".join(f"{k}={v}" for k, v in x.items()) for x in obj) or "(none)"
    return str(obj)
