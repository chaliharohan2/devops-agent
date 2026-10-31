import { useEffect, useState } from "react";
import { local } from "../api";
import type { Job } from "../types";
import { Alert, Badge, Button, Card, ErrorBox, Field, Pre, Spinner, useLoad } from "../ui";

interface ModelOption {
  key: string;
  display: string;
  source: string;
  params: string;
  file_gb: number;
  ram_gb: number;
  license: string;
  notes: string;
}

interface HardwareInfo {
  hardware: { os: string; arch: string; cpu_cores: number; cpu_threads: number; cpu_flags: string[]; total_ram_gb: number; available_ram_gb: number; free_disk_gb: number; gpu: string | null };
  recommended: ModelOption;
  notes: string[];
  catalog: ModelOption[];
}

interface Status {
  server_url: string;
  username: string | null;
  server: string;
  docker: boolean;
  version: string;
  model: { running: boolean; model: string; source: string | null; model_installed?: boolean; installed?: string[]; error?: string };
  repos: Record<string, string>;
}

export function Setup() {
  const status = useLoad(() => local.get<Status>("/status"), [], 10000);
  const hw = useLoad(() => local.get<HardwareInfo>("/hardware"));
  const [job, setJob] = useState<Job | null>(null);
  const [link, setLink] = useState({ target: "", path: "" });
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!job || job.status !== "running") return;
    const t = setInterval(async () => setJob(await local.get<Job>(`/jobs/${job.id}`)), 2000);
    return () => clearInterval(t);
  }, [job]);

  const s = status.data;
  return (
    <div className="stack">
      <Card title="Agent status">
        <ErrorBox error={status.error} />
        {s && (
          <div className="kv">
            <span>Signed in as</span><span>{s.username ?? <Badge value="not registered" tone="bad" />}</span>
            <span>Server agent</span><span>{s.server_url} <Badge value={s.server === "reachable" ? "reachable" : "unreachable"} tone={s.server === "reachable" ? "ok" : "bad"} /></span>
            <span>Docker</span><Badge value={s.docker ? "available" : "not running"} tone={s.docker ? "ok" : "bad"} />
            <span>Local model</span>
            <span>
              <code>{s.model.model}</code>{" "}
              <Badge value={s.model.running ? (s.model.model_installed ? "ready" : "not pulled") : "runtime offline"}
                tone={s.model.running && s.model.model_installed ? "ok" : "warn"} />
              {s.model.source ? <div className="muted small">{s.model.source}</div> : null}
            </span>
            <span>Version</span><span>{s.version}</span>
          </div>
        )}
        {s && !s.username && (
          <Alert tone="warn">Register this machine: <code>devops-agent setup --server URL --token YOUR_TOKEN</code></Alert>
        )}
        {s && !s.model.running && (
          <Alert tone="warn">The local model runtime is not running. Install Ollama (https://ollama.com/download) and start it; the agent falls back to built-in commands meanwhile.</Alert>
        )}
      </Card>

      <Card title="Local model">
        {hw.data && (
          <>
            <div className="kv">
              <span>Machine</span><span>{hw.data.hardware.os} · {hw.data.hardware.arch} · {hw.data.hardware.cpu_cores} cores / {hw.data.hardware.cpu_threads} threads</span>
              <span>Memory</span><span>{hw.data.hardware.total_ram_gb} GB total, {hw.data.hardware.available_ram_gb} GB free</span>
              <span>Disk</span><span>{hw.data.hardware.free_disk_gb} GB free</span>
              <span>CPU features</span><span>{hw.data.hardware.cpu_flags.join(", ") || "—"}</span>
            </div>
            <ul className="small">{hw.data.notes.map((n) => <li key={n}>{n}</li>)}</ul>
            <table className="table">
              <thead><tr><th>Model</th><th>Size</th><th>RAM</th><th>License</th><th /></tr></thead>
              <tbody>
                {hw.data.catalog.map((m) => (
                  <tr key={m.key}>
                    <td><strong>{m.display}</strong> {m.key === hw.data!.recommended.key && <Badge value="recommended" tone="ok" />}<div className="muted small">{m.notes}</div></td>
                    <td>{m.file_gb} GB</td><td>~{m.ram_gb} GB</td><td>{m.license}</td>
                    <td><Button small onClick={async () => {
                      const r = await local.post<{ job: string }>("/model/pull", { key: m.key });
                      setJob({ id: r.job, kind: "model", status: "running", result: null, error: null, log: [] });
                    }}>Use</Button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        {job && (
          <Alert tone={job.status === "failed" ? "bad" : job.status === "done" ? "ok" : "info"}>
            {job.status === "running" && <><Spinner /> Downloading model through Ollama… this can take a while.</>}
            {job.status === "done" && "Model ready."}
            {job.status === "failed" && `Failed: ${job.error}`}
          </Alert>
        )}
      </Card>

      <Card title="Linked checkouts" actions={<Button small onClick={async () => { try { await local.post("/repos/auto-link"); await status.reload(); } catch (e) { setError(e); } }}>Auto-detect</Button>}>
        {s && <Pre>{Object.entries(s.repos).map(([k, v]) => `${k.padEnd(28)} ${v}`).join("\n") || "none"}</Pre>}
        <div className="form-row">
          <Field label="app/component"><input value={link.target} onChange={(e) => setLink({ ...link, target: e.target.value })} placeholder="shop/backend" /></Field>
          <Field label="Path to checkout"><input value={link.path} onChange={(e) => setLink({ ...link, path: e.target.value })} placeholder="~/code/shop-backend" /></Field>
          <div className="form-actions">
            <Button onClick={async () => {
              setError(null);
              const [app, component] = link.target.split("/");
              try { await local.post("/repos/link", { app, component, path: link.path }); await status.reload(); } catch (e) { setError(e); }
            }}>Link</Button>
          </div>
        </div>
        <ErrorBox error={error} />
      </Card>
    </div>
  );
}
