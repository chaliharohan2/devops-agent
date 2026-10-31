"""HTTP client for the server agent API (the only thing the local agent talks to remotely)."""

from __future__ import annotations

import hashlib
import platform
from pathlib import Path
from typing import Any

import httpx

from devops_common.protocol import (
    AgentRegistered,
    AgentRegistration,
    ChangeRequestView,
    DeployRequest,
    EnvironmentSpec,
    InboxEvent,
    PeerRequestCreate,
    PeerRequestView,
)

from . import __version__


class ServerError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"server returned {status}: {detail}")


class ServerClient:
    def __init__(self, base_url: str, token: str | None, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 60.0):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url + "/api/v1", headers=headers, timeout=timeout,
                                  transport=transport)

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ServerError(0, f"cannot reach server at {self.base_url}: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ServerError(r.status_code, str(detail))
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r

    # -- identity -------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._req("GET", "/health")

    def me(self) -> dict[str, Any]:
        return self._req("GET", "/me")

    def register(self, model: str | None, capabilities: dict[str, Any]) -> AgentRegistered:
        body = AgentRegistration(hostname=platform.node() or "unknown", version=__version__, model=model,
                                 capabilities=capabilities)
        return AgentRegistered.model_validate(self._req("POST", "/agents/register", json=body.model_dump()))

    def heartbeat(self, model: str | None, capabilities: dict[str, Any]) -> None:
        body = AgentRegistration(hostname=platform.node() or "unknown", version=__version__, model=model,
                                 capabilities=capabilities)
        self._req("POST", "/agents/heartbeat", json=body.model_dump())

    def inbox(self, after: int) -> list[InboxEvent]:
        return [InboxEvent.model_validate(e) for e in self._req("GET", "/agents/inbox", params={"after": after})]

    # -- apps & environments ----------------------------------------------------
    def apps(self) -> list[dict[str, Any]]:
        return self._req("GET", "/apps")

    def app(self, name: str) -> dict[str, Any]:
        return self._req("GET", f"/apps/{name}")

    def spec(self, app: str, environment: str) -> EnvironmentSpec:
        return EnvironmentSpec.model_validate(self._req("GET", f"/apps/{app}/environments/{environment}/spec"))

    # -- change requests --------------------------------------------------------
    def create_change_request(self, req: DeployRequest) -> ChangeRequestView:
        return ChangeRequestView.model_validate(self._req("POST", "/change-requests", json=req.model_dump(mode="json")))

    def change_request(self, cr_id: int) -> ChangeRequestView:
        return ChangeRequestView.model_validate(self._req("GET", f"/change-requests/{cr_id}"))

    def my_change_requests(self, limit: int = 20) -> list[ChangeRequestView]:
        return [ChangeRequestView.model_validate(x)
                for x in self._req("GET", "/change-requests", params={"mine": True, "limit": limit})]

    # -- peers ------------------------------------------------------------------
    def peer_create(self, req: PeerRequestCreate) -> PeerRequestView:
        return PeerRequestView.model_validate(self._req("POST", "/peers", json=req.model_dump(mode="json")))

    def peers(self) -> list[PeerRequestView]:
        return [PeerRequestView.model_validate(x) for x in self._req("GET", "/peers")]

    def peer_respond(self, pr_id: int, approve: bool) -> PeerRequestView:
        return PeerRequestView.model_validate(
            self._req("POST", f"/peers/{pr_id}/respond", params={"approve": str(approve).lower()}))

    def peer_upload(self, pr_id: int, data: bytes, head_sha: str | None) -> PeerRequestView:
        headers = {"Content-Type": "application/octet-stream", **({"X-Head-Sha": head_sha} if head_sha else {})}
        return PeerRequestView.model_validate(self._req("PUT", f"/peers/{pr_id}/artifact", content=data, headers=headers))

    def peer_download(self, pr_id: int, dest: Path) -> tuple[Path, dict[str, str]]:
        r = self._req("GET", f"/peers/{pr_id}/artifact")
        data = r.content
        expected = r.headers.get("X-Artifact-Sha256")
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise ServerError(0, "artifact checksum mismatch")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest, {"head_sha": r.headers.get("X-Head-Sha", ""), "share": r.headers.get("X-Share-Kind", "")}
