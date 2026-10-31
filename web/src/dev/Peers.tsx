import { useState } from "react";
import { local } from "../api";
import type { App, PeerRequest } from "../types";
import { Alert, Badge, Button, Card, Empty, ErrorBox, Field, timeAgo, useLoad } from "../ui";

export function Peers({ me }: { me: string | null }) {
  const apps = useLoad(() => local.get<App[]>("/apps"));
  const peers = useLoad(() => local.get<PeerRequest[]>("/peers"), [], 4000);
  const [target, setTarget] = useState("");
  const [from, setFrom] = useState("");
  const [branch, setBranch] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState<unknown>(null);
  const options = (apps.data ?? []).flatMap((a) => a.components.map((c) => `${a.name}/${c.name}`));
  const incoming = (peers.data ?? []).filter((p) => p.to_user === me);
  const outgoing = (peers.data ?? []).filter((p) => p.from_user === me);

  async function request() {
    setError(null);
    const [app, component] = (target || options[0] || "/").split("/");
    try {
      await local.post("/peers", { app, component, from_user: from, branch: branch || null, message });
      setFrom(""); setBranch(""); setMessage("");
      await peers.reload();
    } catch (e) {
      setError(e);
    }
  }

  async function respond(id: number, approve: boolean) {
    setError(null);
    try {
      await local.post(`/peers/${id}/respond?approve=${approve}`);
      await peers.reload();
    } catch (e) {
      setError(e);
    }
  }

  return (
    <div className="stack">
      <Alert tone="info">
        Phase 2 preview: test against a teammate's code that is not deployed yet. Their agent shares a branch (git
        bundle) or their current working tree — only after they approve. Transfers go through the server and expire
        after 24h.
      </Alert>
      <Card title="Ask a teammate for their code">
        <div className="form-row">
          <Field label="Component">
            <select value={target || options[0]} onChange={(e) => setTarget(e.target.value)}>
              {options.map((o) => <option key={o}>{o}</option>)}
            </select>
          </Field>
          <Field label="Teammate (username)"><input value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="Branch" hint="leave empty to ask for their working tree"><input value={branch} onChange={(e) => setBranch(e.target.value)} /></Field>
        </div>
        <Field label="Message"><input value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Want to test my backend against your new checkout UI" /></Field>
        <div className="form-actions"><Button kind="primary" onClick={request} disabled={!from}>Send request</Button></div>
        <ErrorBox error={error} />
      </Card>
      <div className="split">
        <Card title="Requests to me">
          {incoming.length === 0 && <Empty>No one has asked for your code.</Empty>}
          <ul className="list">
            {incoming.map((p) => (
              <li key={p.id}>
                <div className="list-main">
                  <strong>{p.from_user}</strong> wants {p.app}/{p.component} ({p.share === "branch" ? `branch ${p.ref}` : "your working tree"})
                  <div className="muted small">{p.message} · {timeAgo(p.created_at)}</div>
                </div>
                {p.status === "pending" ? (
                  <span className="row">
                    <Button small kind="primary" onClick={() => respond(p.id, true)}>Share</Button>
                    <Button small onClick={() => respond(p.id, false)}>Deny</Button>
                  </span>
                ) : <Badge value={p.status} />}
              </li>
            ))}
          </ul>
        </Card>
        <Card title="My requests">
          {outgoing.length === 0 && <Empty>None yet.</Empty>}
          <ul className="list">
            {outgoing.map((p) => (
              <li key={p.id}>
                <div className="list-main">
                  #{p.id} {p.app}/{p.component} from <strong>{p.to_user}</strong>
                  <div className="muted small">{p.share === "branch" ? p.ref : "working tree"}{p.head_sha ? ` @ ${p.head_sha.slice(0, 10)}` : ""}</div>
                </div>
                <Badge value={p.status} />
              </li>
            ))}
          </ul>
          <div className="muted small">Ready shares appear as options in Test environments.</div>
        </Card>
      </div>
    </div>
  );
}
