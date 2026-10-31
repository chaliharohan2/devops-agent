import { useState } from "react";
import { server } from "../api";
import { ChangeRequestDetail } from "../dev/Deploys";
import type { ChangeRequest, Deployment } from "../types";
import { Alert, Badge, Button, Card, Empty, ErrorBox, Field, Pre, Sha, timeAgo, useLoad } from "../ui";

export function ChangeRequests({ selected, onSelect, role }: { selected: number | null; onSelect: (id: number) => void; role: string }) {
  const [filter, setFilter] = useState("");
  const list = useLoad(() => server.get<ChangeRequest[]>(`/change-requests?limit=100${filter ? `&status=${filter}` : ""}`), [filter], 4000);
  const current = list.data?.find((c) => c.id === selected) ?? list.data?.[0];
  const [reason, setReason] = useState("");
  const [override, setOverride] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const canDecide = role === "maintainer" || role === "admin";

  return (
    <div className="split">
      <Card title="Change requests" actions={
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">all</option>
          {["awaiting_approval", "deployed", "failed", "conflict", "rejected"].map((s) => <option key={s}>{s}</option>)}
        </select>
      }>
        <ErrorBox error={list.error} />
        {list.data?.length === 0 && <Empty>No change requests.</Empty>}
        <ul className="list">
          {list.data?.map((c) => (
            <li key={c.id} className={current?.id === c.id ? "active" : ""} onClick={() => onSelect(c.id)}>
              <div className="list-main">#{c.id} {c.app}/{c.component} → {c.environment}
                <div className="muted small">{c.requested_by} · {c.source_branch} · {timeAgo(c.created_at)}</div></div>
              <span className="row">{c.risk_level && <Badge value={c.risk_level} />}<Badge value={c.status} /></span>
            </li>
          ))}
        </ul>
      </Card>
      <Card title={current ? `#${current.id} by ${current.requested_by}` : "Details"}>
        {current ? (
          <div className="stack-sm">
            <ChangeRequestDetail cr={current} />
            {current.status === "awaiting_approval" && canDecide && (
              <div className="decision">
                <Alert tone="info">You are approving a merge of <code>{current.source_branch}</code> into <code>{current.target_branch}</code> and a deploy to <strong>{current.environment}</strong>. The server agent will never modify code.</Alert>
                <label className="check"><input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} /> approve even if config values are still missing</label>
                <div className="row">
                  <Button kind="primary" onClick={async () => {
                    setError(null);
                    try { await server.post(`/change-requests/${current.id}/approve`, { override_missing: override }); await list.reload(); } catch (e) { setError(e); }
                  }}>Approve & deploy</Button>
                </div>
                <Field label="Reject with reason"><input value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
                <Button kind="danger" disabled={!reason} onClick={async () => {
                  setError(null);
                  try { await server.post(`/change-requests/${current.id}/reject`, { reason }); setReason(""); await list.reload(); } catch (e) { setError(e); }
                }}>Reject</Button>
                <ErrorBox error={error} />
              </div>
            )}
          </div>
        ) : <Empty>Select a request.</Empty>}
      </Card>
    </div>
  );
}

export function Deployments({ selected, onSelect }: { selected: number | null; onSelect: (id: number) => void }) {
  const list = useLoad(() => server.get<Deployment[]>("/deployments?limit=100"), [], 5000);
  const current = list.data?.find((d) => d.id === selected) ?? list.data?.[0];
  return (
    <div className="split">
      <Card title="Deployments">
        <ErrorBox error={list.error} />
        {list.data?.length === 0 && <Empty>No deployments yet.</Empty>}
        <ul className="list">
          {list.data?.map((d) => (
            <li key={d.id} className={current?.id === d.id ? "active" : ""} onClick={() => onSelect(d.id)}>
              <div className="list-main">#{d.id} {d.app} → {d.environment}
                <div className="muted small">{d.target} · by {d.triggered_by} · {timeAgo(d.created_at)}</div></div>
              <Badge value={d.status} />
            </li>
          ))}
        </ul>
      </Card>
      <Card title={current ? `Deployment #${current.id}` : "Details"}>
        {current ? (
          <div className="stack-sm">
            <div className="kv">
              <span>Status</span><Badge value={current.status} />
              <span>Target</span><span>{current.target}</span>
              <span>Config version</span><span>v{current.config_version}</span>
              <span>Change request</span><span>{current.change_request_id ? `#${current.change_request_id}` : "manual"}</span>
              {Object.entries(current.component_shas).map(([k, v]) => [<span key={k + "k"}>{k}</span>, <Sha key={k + "v"} sha={v} />])}
            </div>
            {current.diagnosis && <Alert tone="warn"><strong>Server agent diagnosis</strong><div className="prewrap">{current.diagnosis}</div></Alert>}
            <details open><summary>Log</summary><Pre maxHeight={300}>{current.log || "(empty)"}</Pre></details>
            <details><summary>Rendered plan (secrets masked)</summary><Pre maxHeight={400}>{JSON.stringify(current.plan, null, 2)}</Pre></details>
          </div>
        ) : <Empty>Select a deployment.</Empty>}
      </Card>
    </div>
  );
}
