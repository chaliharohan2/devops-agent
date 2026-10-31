import { useMemo, useState } from "react";
import { local } from "../api";
import type { Analysis, App, ChangeRequest } from "../types";
import { Alert, Badge, Button, Card, Empty, ErrorBox, Field, Sha, Timeline, useLoad } from "../ui";

interface Value {
  value: string;
  secret: boolean;
}

export function Deploy({ onOpenRequest }: { onOpenRequest: (id: number) => void }) {
  const apps = useLoad(() => local.get<App[]>("/apps"));
  const repos = useLoad(() => local.get<Record<string, string>>("/repos"));
  const linked = useMemo(() => Object.keys(repos.data ?? {}), [repos.data]);
  const [target, setTarget] = useState("");
  const [env, setEnv] = useState("");
  const [branch, setBranch] = useState("");
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [values, setValues] = useState<Record<string, Value>>({});
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [submitted, setSubmitted] = useState<ChangeRequest | null>(null);

  const selectedTarget = target || linked[0] || "";
  const [appName, component] = selectedTarget.split("/");
  const app = apps.data?.find((a) => a.name === appName);
  const envName = env || app?.environments[0]?.name || "";

  async function analyze() {
    setError(null);
    setSubmitted(null);
    try {
      const res = await local.post<Analysis>("/changes/analyze", {
        app: appName, component, environment: envName, branch: branch || null,
      });
      setAnalysis(res);
      const v: Record<string, Value> = {};
      for (const n of res.values_needed ?? []) v[n.key] = { value: n.default ?? "", secret: n.secret };
      setValues(v);
    } catch (e) {
      setError(e);
    }
  }

  async function submit() {
    setError(null);
    try {
      const cr = await local.post<ChangeRequest>("/deploys", {
        app: appName, component, environment: envName, branch: branch || null, notes,
        config_values: Object.entries(values)
          .filter(([, v]) => v.value !== "")
          .map(([key, v]) => ({ key, value: v.value, is_secret: v.secret, component })),
      });
      setSubmitted(cr);
    } catch (e) {
      setError(e);
    }
  }

  const report = analysis?.report;
  const pf = analysis?.preflight;

  return (
    <div className="stack">
      <Card title="Deploy a pushed branch">
        <p className="muted">
          Push your branch first — the agent only deploys what is on the remote. It analyses the diff against the
          environment's branch, asks you for any new config, then the server agent merges and deploys.
        </p>
        {linked.length === 0 ? (
          <Alert tone="warn">No local checkouts linked yet. Link them in Setup.</Alert>
        ) : (
          <div className="form-row">
            <Field label="Component">
              <select value={selectedTarget} onChange={(e) => { setTarget(e.target.value); setAnalysis(null); }}>
                {linked.map((k) => <option key={k}>{k}</option>)}
              </select>
            </Field>
            <Field label="Environment">
              <select value={envName} onChange={(e) => { setEnv(e.target.value); setAnalysis(null); }}>
                {app?.environments.map((e) => (
                  <option key={e.name} value={e.name}>
                    {e.name} ({e.branch}{e.requires_approval ? ", approval" : ""})
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Branch" hint="default: current branch">
              <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="feature/…" />
            </Field>
            <div className="form-actions">
              <Button kind="primary" onClick={analyze} disabled={!envName}>Analyze</Button>
            </div>
          </div>
        )}
        <ErrorBox error={error ?? apps.error} />
      </Card>

      {pf && (
        <Card title={`Preflight: ${pf.branch ?? "?"} → ${analysis?.target_branch}`}>
          {pf.problems.map((p) => <Alert key={p} tone="bad">{p}</Alert>)}
          {pf.warnings.map((w) => <Alert key={w} tone="warn">{w}</Alert>)}
          {!pf.problems.length && !pf.warnings.length && <Alert tone="ok">Branch is pushed and up to date.</Alert>}
          <div className="kv">
            <span>Pushed head</span><Sha sha={pf.pushed_head} />
            <span>Local head</span><Sha sha={pf.local_head} />
          </div>
        </Card>
      )}

      {report && (
        <Card title="What this change means operationally">
          <p>{report.summary}</p>
          {report.warnings.map((w) => <Alert key={w} tone="warn">{w}</Alert>)}
          {report.config_changes.length === 0 ? (
            <Empty>No configuration impact detected.</Empty>
          ) : (
            <table className="table">
              <thead><tr><th>Change</th><th>Key / file</th><th>Evidence</th><th /></tr></thead>
              <tbody>
                {report.config_changes.map((c, i) => (
                  <tr key={i}>
                    <td><Badge value={c.kind} tone="info" /></td>
                    <td><code>{c.key}</code><div className="muted small">{c.description}</div></td>
                    <td className="small">{c.evidence.slice(0, 2).map((e) => `${e.file}${e.line ? `:${e.line}` : ""}`).join(", ")}</td>
                    <td>{c.is_secret_hint ? <Badge value="secret" tone="warn" /> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <details>
            <summary>{report.commits.length} commit(s), {report.changed_files.length} file(s)</summary>
            <ul className="small">
              {report.commits.map((c) => <li key={c.sha}><Sha sha={c.sha} /> {c.subject} — {c.author}</li>)}
            </ul>
          </details>
        </Card>
      )}

      {report && !pf?.problems.length && (
        <Card title={`Config values for ${envName}`}>
          {Object.keys(values).length === 0 ? (
            <Empty>No new config needed.</Empty>
          ) : (
            <div className="stack-sm">
              {analysis?.values_needed?.map((n) => (
                <Field key={n.key} label={n.key} hint={`${n.description}${n.required ? "" : " (has a default)"} · ${n.evidence.join(", ")}`}>
                  <input
                    type={values[n.key]?.secret ? "password" : "text"}
                    value={values[n.key]?.value ?? ""}
                    placeholder={n.required ? "required — or leave blank for a maintainer to set" : n.default ?? ""}
                    onChange={(e) => setValues({ ...values, [n.key]: { ...values[n.key], value: e.target.value } })}
                  />
                </Field>
              ))}
            </div>
          )}
          <Field label="Notes for reviewers">
            <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} />
          </Field>
          <div className="form-actions">
            <Button kind="primary" onClick={submit}>Request deploy</Button>
          </div>
        </Card>
      )}

      {submitted && (
        <Card title={`Change request #${submitted.id}`} actions={<Button onClick={() => onOpenRequest(submitted.id)}>Follow</Button>}>
          <Badge value={submitted.status} />
          <Timeline items={submitted.timeline} />
        </Card>
      )}
    </div>
  );
}
