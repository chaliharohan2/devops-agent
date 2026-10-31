import { useState } from "react";
import { server } from "../api";
import type { App, ConfigEntry } from "../types";
import { Badge, Button, Card, Empty, ErrorBox, Field, timeAgo, useLoad } from "../ui";

interface Revision {
  version: number;
  actor: string;
  message: string;
  change_request_id: number | null;
  changes: { action: string; scope: string; key: string; old: string | null; new: string | null }[];
  created_at: string;
}

export function Configs({ role }: { role: string }) {
  const apps = useLoad(() => server.get<App[]>("/apps"));
  const [appName, setAppName] = useState("");
  const [envName, setEnvName] = useState("");
  const app = apps.data?.find((a) => a.name === (appName || apps.data?.[0]?.name));
  const env = envName || app?.environments[0]?.name || "";
  const entries = useLoad(() => (app && env ? server.get<ConfigEntry[]>(`/apps/${app.name}/environments/${env}/configs`) : Promise.resolve([])), [app?.name, env]);
  const history = useLoad(() => (app && env ? server.get<Revision[]>(`/apps/${app.name}/environments/${env}/configs/history`) : Promise.resolve([])), [app?.name, env]);
  const [form, setForm] = useState({ key: "", value: "", scope: "", secret: false, message: "" });
  const [error, setError] = useState<unknown>(null);
  const canEdit = role === "maintainer" || role === "admin";

  async function save() {
    if (!app) return;
    setError(null);
    try {
      await server.put(`/apps/${app.name}/environments/${env}/configs`, {
        values: [{ key: form.key, value: form.value, is_secret: form.secret, component: form.scope || null }],
        message: form.message,
      });
      setForm({ key: "", value: "", scope: "", secret: false, message: "" });
      await Promise.all([entries.reload(), history.reload()]);
    } catch (e) {
      setError(e);
    }
  }

  return (
    <div className="stack">
      <Card title="Environment configuration" actions={
        <span className="row">
          <select value={app?.name ?? ""} onChange={(e) => { setAppName(e.target.value); setEnvName(""); }}>
            {apps.data?.map((a) => <option key={a.name}>{a.name}</option>)}
          </select>
          <select value={env} onChange={(e) => setEnvName(e.target.value)}>
            {app?.environments.map((e) => <option key={e.name}>{e.name}</option>)}
          </select>
        </span>
      }>
        <p className="muted small">Config lives in the server agent's store (not in repos). Secrets are encrypted at rest and never shown or sent to laptops. Component scope overrides app-wide (*) values.</p>
        <ErrorBox error={entries.error} />
        {entries.data?.length === 0 ? <Empty>No config yet.</Empty> : (
          <table className="table">
            <thead><tr><th>Scope</th><th>Key</th><th>Value</th><th>Version</th><th>Updated</th></tr></thead>
            <tbody>
              {entries.data?.map((e) => (
                <tr key={e.scope + e.key}>
                  <td><code>{e.scope}</code></td>
                  <td><code>{e.key}</code> {e.is_secret && <Badge value="secret" tone="warn" />}</td>
                  <td className="mono">{e.value}</td>
                  <td>v{e.version}</td>
                  <td className="small">{e.updated_by} · {timeAgo(e.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {canEdit && (
          <div className="form-row">
            <Field label="Key"><input value={form.key} onChange={(e) => setForm({ ...form, key: e.target.value.toUpperCase() })} placeholder="PAYMENT_API_KEY" /></Field>
            <Field label="Value"><input type={form.secret ? "password" : "text"} value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} /></Field>
            <Field label="Scope">
              <select value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}>
                <option value="">* (app-wide)</option>
                {app?.components.map((c) => <option key={c.name}>{c.name}</option>)}
              </select>
            </Field>
            <label className="check"><input type="checkbox" checked={form.secret} onChange={(e) => setForm({ ...form, secret: e.target.checked })} /> secret</label>
            <div className="form-actions"><Button kind="primary" onClick={save} disabled={!form.key}>Save</Button></div>
          </div>
        )}
        <ErrorBox error={error} />
      </Card>
      <Card title="History">
        {history.data?.length === 0 && <Empty>No changes yet.</Empty>}
        <ul className="list">
          {history.data?.map((r) => (
            <li key={r.version}>
              <div className="list-main">
                <strong>v{r.version}</strong> by {r.actor} {r.change_request_id ? `(change request #${r.change_request_id})` : ""} <span className="muted small">{timeAgo(r.created_at)}</span>
                <div className="small">{r.changes.map((c) => `${c.action} ${c.scope}:${c.key}`).join(", ")}</div>
              </div>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

export function ServerChat() {
  const [messages, setMessages] = useState<{ role: "user" | "assistant"; content: string }[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function send() {
    const next = [...messages, { role: "user" as const, content: input }];
    setMessages(next);
    setInput("");
    setBusy(true);
    setError(null);
    try {
      const res = await server.post<{ reply: string; steps: { tool: string }[] }>("/agent/chat", { messages: next });
      setMessages([...next, { role: "assistant", content: res.reply + (res.steps.length ? `\n\n(used: ${res.steps.map((s) => s.tool).join(", ")})` : "") }]);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Ask the server agent" className="chat-card">
      <p className="muted small">Answers come from live data via read-only tools (vLLM). It can explain failures and summarise activity; approvals stay in the dashboard.</p>
      <div className="chat">
        {messages.map((m, i) => <div key={i} className={`bubble bubble-${m.role === "user" ? "user" : "agent"}`}><div className="bubble-text">{m.content}</div></div>)}
        {busy && <div className="bubble bubble-agent muted">Thinking…</div>}
      </div>
      <ErrorBox error={error} />
      <form className="chat-input" onSubmit={(e) => { e.preventDefault(); if (input.trim()) void send(); }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Why did the last staging deployment fail?" />
        <Button kind="primary" type="submit" disabled={busy || !input.trim()}>Send</Button>
      </form>
    </Card>
  );
}
