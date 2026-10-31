import { useState } from "react";
import { server, setToken } from "../api";
import type { AgentInfo, App, AuditEvent, ChangeRequest, Deployment } from "../types";
import { Badge, Button, Card, Empty, ErrorBox, Field, Pre, timeAgo, useLoad } from "../ui";

export function Login({ onLogin }: { onLogin: (token: string) => void }) {
  const [token, setTok] = useState("");
  const [error, setError] = useState<unknown>(null);
  return (
    <div className="login">
      <Card title="Server agent dashboard">
        <p className="muted">Sign in with your API token (ask an admin, or run <code>devops-server create-user</code>).</p>
        <form onSubmit={async (e) => {
          e.preventDefault();
          setError(null);
          try {
            const res = await fetch("/api/v1/me", { headers: { Authorization: `Bearer ${token}` } });
            if (!res.ok) throw new Error("Invalid token");
            setToken(token);
            onLogin(token);
          } catch (err) { setError(err); }
        }}>
          <Field label="API token"><input type="password" value={token} onChange={(e) => setTok(e.target.value)} autoFocus /></Field>
          <div className="form-actions"><Button kind="primary" type="submit" disabled={!token}>Sign in</Button></div>
          <ErrorBox error={error} />
        </form>
      </Card>
    </div>
  );
}

export function Overview({ go }: { go: (page: string, id?: number) => void }) {
  const apps = useLoad(() => server.get<App[]>("/apps"));
  const crs = useLoad(() => server.get<ChangeRequest[]>("/change-requests?limit=8"), [], 5000);
  const deps = useLoad(() => server.get<Deployment[]>("/deployments?limit=8"), [], 5000);
  const agents = useLoad(() => server.get<AgentInfo[]>("/agents"), [], 15000);
  const llm = useLoad(() => server.get<{ ok: boolean; configured: boolean; models?: string[]; error?: string; base_url?: string }>("/llm/health"));
  const waiting = crs.data?.filter((c) => c.status === "awaiting_approval") ?? [];

  return (
    <div className="stack">
      <div className="stats">
        <div className="stat"><div className="stat-value">{apps.data?.length ?? "–"}</div><div className="stat-label">apps</div></div>
        <div className="stat"><div className="stat-value">{waiting.length}</div><div className="stat-label">awaiting approval</div></div>
        <div className="stat"><div className="stat-value">{agents.data?.filter((a) => a.online).length ?? "–"}</div><div className="stat-label">agents online</div></div>
        <div className="stat">
          <div className="stat-value"><Badge value={llm.data?.ok ? "online" : "offline"} tone={llm.data?.ok ? "ok" : "bad"} /></div>
          <div className="stat-label">server LLM {llm.data?.models?.[0] ? `· ${llm.data.models[0]}` : ""}</div>
        </div>
      </div>
      {llm.data && !llm.data.ok && (
        <ErrorBox error={`Server LLM unavailable (${llm.data.error ?? "not configured"}). Reviews fall back to deterministic analysis.`} />
      )}
      <div className="split">
        <Card title="Recent change requests" actions={<Button small onClick={() => go("changes")}>All</Button>}>
          {crs.data?.length === 0 && <Empty>None yet.</Empty>}
          <ul className="list">
            {crs.data?.map((c) => (
              <li key={c.id} onClick={() => go("changes", c.id)}>
                <div className="list-main">#{c.id} {c.app}/{c.component} → {c.environment}
                  <div className="muted small">{c.requested_by} · {c.source_branch} · {timeAgo(c.created_at)}</div></div>
                <Badge value={c.status} />
              </li>
            ))}
          </ul>
        </Card>
        <Card title="Recent deployments" actions={<Button small onClick={() => go("deployments")}>All</Button>}>
          {deps.data?.length === 0 && <Empty>None yet.</Empty>}
          <ul className="list">
            {deps.data?.map((d) => (
              <li key={d.id} onClick={() => go("deployments", d.id)}>
                <div className="list-main">#{d.id} {d.app} → {d.environment}
                  <div className="muted small">{d.target} · config v{d.config_version} · {timeAgo(d.created_at)}</div></div>
                <Badge value={d.status} />
              </li>
            ))}
          </ul>
        </Card>
      </div>
      <Card title="Apps">
        <table className="table">
          <thead><tr><th>App</th><th>Components</th><th>Environments</th></tr></thead>
          <tbody>
            {apps.data?.map((a) => (
              <tr key={a.id}>
                <td><strong>{a.name}</strong><div className="muted small">{a.description}</div></td>
                <td>{a.components.map((c) => <div key={c.id}>{c.name} <span className="muted small">{c.kind} · {c.repo_url}</span></div>)}</td>
                <td>{a.environments.map((e) => <div key={e.id}>{e.name} <span className="muted small">{e.branch} · cfg v{e.config_version}{e.requires_approval ? " · approval" : ""}{e.auto_deploy ? "" : " · manual"}</span></div>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <Card title="Developer agents">
        <table className="table">
          <thead><tr><th>Developer</th><th>Host</th><th>Model</th><th>Last seen</th></tr></thead>
          <tbody>
            {agents.data?.map((a) => (
              <tr key={a.id}><td>{a.username}</td><td>{a.hostname}</td><td className="small">{a.model ?? "—"}</td>
                <td><Badge value={a.online ? "online" : "offline"} tone={a.online ? "ok" : "muted"} /> <span className="muted small">{timeAgo(a.last_seen_at)}</span></td></tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

export function Audit() {
  const events = useLoad(() => server.get<AuditEvent[]>("/audit?limit=200"), [], 10000);
  return (
    <Card title="Audit log">
      <ErrorBox error={events.error} />
      <table className="table">
        <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Subject</th><th>Details</th></tr></thead>
        <tbody>
          {events.data?.map((e) => (
            <tr key={e.id}>
              <td className="small">{new Date(e.ts).toLocaleString()}</td>
              <td>{e.actor}</td>
              <td><code>{e.action}</code></td>
              <td className="small">{e.subject_type}:{e.subject_id}</td>
              <td><details><summary className="small">view</summary><Pre maxHeight={200}>{JSON.stringify(e.details, null, 2)}</Pre></details></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}
