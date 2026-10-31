import { useEffect, useMemo, useState } from "react";
import { local } from "../api";
import type { App, EnvState, Job, PeerRequest } from "../types";
import { Alert, Badge, Button, Card, Empty, ErrorBox, Field, Pre, Spinner, timeAgo, useLoad } from "../ui";

export function Envs() {
  const apps = useLoad(() => local.get<App[]>("/apps"));
  const repos = useLoad(() => local.get<Record<string, string>>("/repos"));
  const envs = useLoad(() => local.get<EnvState[]>("/envs"), [], 3000);
  const peers = useLoad(() => local.get<PeerRequest[]>("/peers"));
  const [appName, setAppName] = useState("");
  const [envName, setEnvName] = useState("");
  const [localComps, setLocalComps] = useState<string[]>([]);
  const [peerPick, setPeerPick] = useState<Record<string, number>>({});
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [logs, setLogs] = useState<string>("");

  const app = apps.data?.find((a) => a.name === (appName || apps.data?.[0]?.name));
  const linkedComps = useMemo(
    () => Object.keys(repos.data ?? {}).filter((k) => k.startsWith(`${app?.name}/`)).map((k) => k.split("/")[1]),
    [repos.data, app],
  );
  const readyPeers = (peers.data ?? []).filter((p) => p.app === app?.name && ["uploaded", "fetched"].includes(p.status));

  useEffect(() => {
    if (!job || job.status !== "running") return;
    const t = setInterval(async () => {
      const j = await local.get<Job>(`/jobs/${job.id}`);
      setJob(j);
      if (j.status !== "running") void envs.reload();
    }, 1500);
    return () => clearInterval(t);
  }, [job, envs]);

  async function start() {
    setError(null);
    try {
      const res = await local.post<{ job: string }>("/envs", {
        app: app?.name, environment: envName || app?.environments[0]?.name || "dev",
        local_components: localComps, peer_requests: peerPick,
      });
      setJob({ id: res.job, kind: "env", status: "running", result: null, error: null, log: [] });
    } catch (e) {
      setError(e);
    }
  }

  const current = envs.data?.find((e) => e.name === selected) ?? envs.data?.[0];

  return (
    <div className="stack">
      <Card title="New end-to-end test environment">
        <p className="muted">
          Your selected components run from your checkout with live reload (uncommitted changes included). Every
          other component runs the exact commit the chosen environment tracks, plus its databases and services —
          all in Docker on this machine. Secrets are stubbed.
        </p>
        <div className="form-row">
          <Field label="App">
            <select value={app?.name ?? ""} onChange={(e) => { setAppName(e.target.value); setLocalComps([]); }}>
              {apps.data?.map((a) => <option key={a.name}>{a.name}</option>)}
            </select>
          </Field>
          <Field label="Based on environment">
            <select value={envName || app?.environments[0]?.name || ""} onChange={(e) => setEnvName(e.target.value)}>
              {app?.environments.map((e) => <option key={e.name} value={e.name}>{e.name} ({e.branch})</option>)}
            </select>
          </Field>
        </div>
        <div className="components-grid">
          {app?.components.map((c) => {
            const isLinked = linkedComps.includes(c.name);
            const peer = readyPeers.filter((p) => p.component === c.name);
            return (
              <div key={c.name} className="component-tile">
                <div className="strong">{c.name} <span className="muted small">{c.kind}</span></div>
                <label className="check">
                  <input type="checkbox" disabled={!isLinked} checked={localComps.includes(c.name)}
                    onChange={(e) => setLocalComps(e.target.checked ? [...localComps, c.name] : localComps.filter((x) => x !== c.name))} />
                  my local code {isLinked ? "" : "(not linked)"}
                </label>
                {peer.length > 0 && (
                  <select value={peerPick[c.name] ?? ""} onChange={(e) => {
                    const v = e.target.value;
                    const next = { ...peerPick };
                    if (v) next[c.name] = Number(v); else delete next[c.name];
                    setPeerPick(next);
                  }}>
                    <option value="">server version</option>
                    {peer.map((p) => <option key={p.id} value={p.id}>{p.to_user}'s {p.share === "branch" ? p.ref : "working tree"} (#{p.id})</option>)}
                  </select>
                )}
              </div>
            );
          })}
        </div>
        <div className="form-actions">
          <Button kind="primary" onClick={start} disabled={job?.status === "running" || !app}>
            Build & start
          </Button>
        </div>
        <ErrorBox error={error ?? apps.error} />
        {job && (
          <Alert tone={job.status === "failed" ? "bad" : job.status === "done" ? "ok" : "info"}>
            {job.status === "running" ? <><Spinner /> Preparing sources, building images and waiting for health checks… (first run can take a few minutes)</> : null}
            {job.status === "failed" ? `Failed: ${job.error}` : null}
            {job.status === "done" ? "Environment ready." : null}
            {job.log.length > 0 && <Pre maxHeight={120}>{job.log.join("\n")}</Pre>}
          </Alert>
        )}
      </Card>

      <div className="split">
        <Card title="Environments on this machine">
          {envs.data?.length === 0 && <Empty>No environments yet.</Empty>}
          <ul className="list">
            {envs.data?.map((e) => (
              <li key={e.name} className={current?.name === e.name ? "active" : ""} onClick={() => { setSelected(e.name); setLogs(""); }}>
                <div className="list-main">
                  <strong>{e.name}</strong>
                  <div className="muted small">local: {e.local_components.join(", ") || "none"} · {timeAgo(e.updated_at)}</div>
                </div>
                <Badge value={e.status} />
              </li>
            ))}
          </ul>
        </Card>
        <Card
          title={current?.name ?? "Details"}
          actions={current && (
            <>
              <Button small onClick={async () => setLogs((await local.get<{ logs: string }>(`/envs/${current.name}/logs?tail=200`)).logs)}>Logs</Button>
              <Button small kind="danger" onClick={async () => { await local.del(`/envs/${current.name}`); await envs.reload(); }}>Tear down</Button>
            </>
          )}
        >
          {current ? (
            <div className="stack-sm">
              {current.message && <div className="muted small">{current.message}</div>}
              <div className="url-list">
                {Object.entries(current.urls).map(([k, u]) => (
                  <a key={k} href={u} target="_blank" rel="noreferrer" className="url-pill">
                    {k} <span className="muted">{u}</span> {current.health[k] ? <Badge value={current.health[k]} /> : null}
                  </a>
                ))}
              </div>
              <Pre>{current.instructions}</Pre>
              {logs && <Pre maxHeight={320}>{logs}</Pre>}
            </div>
          ) : <Empty>Nothing selected.</Empty>}
        </Card>
      </div>
    </div>
  );
}
