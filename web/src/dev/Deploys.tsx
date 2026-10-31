import { local } from "../api";
import type { ChangeRequest } from "../types";
import { Alert, Badge, Card, Empty, ErrorBox, Sha, Timeline, timeAgo, useLoad } from "../ui";

export function ChangeRequestDetail({ cr }: { cr: ChangeRequest }) {
  return (
    <div className="stack-sm">
      <div className="kv">
        <span>Status</span><Badge value={cr.status} />
        <span>Risk</span>{cr.risk_level ? <Badge value={cr.risk_level} /> : <span className="muted">—</span>}
        <span>Branch</span><span>{cr.source_branch} → {cr.target_branch}</span>
        <span>Head</span><Sha sha={cr.head_sha} />
        <span>Merge</span><Sha sha={cr.merge_sha} />
        <span>Config keys</span><span>{cr.config_keys.join(", ") || "—"}</span>
      </div>
      {cr.error && <Alert tone="bad"><div className="prewrap">{cr.error}</div></Alert>}
      {cr.discrepancies.length > 0 && (
        <Alert tone="warn">
          <strong>Local and server analyses disagree:</strong>
          <ul>{cr.discrepancies.map((d) => <li key={d}>{d}</li>)}</ul>
        </Alert>
      )}
      {cr.review && (
        <div className="review">
          <div className="review-head">
            Server agent review <span className="muted small">({cr.review.source})</span>
          </div>
          <p>{cr.review.summary}</p>
          {cr.review.concerns.length > 0 && (
            <>
              <div className="small strong">Concerns</div>
              <ul>{cr.review.concerns.map((c) => <li key={c}>{c}</li>)}</ul>
            </>
          )}
          {cr.review.missing_config.length > 0 && (
            <Alert tone="warn">Missing config: {cr.review.missing_config.join(", ")}</Alert>
          )}
          {cr.review.deployment_steps.length > 0 && (
            <>
              <div className="small strong">Rollout plan</div>
              <ol>{cr.review.deployment_steps.map((s) => <li key={s}>{s}</li>)}</ol>
            </>
          )}
        </div>
      )}
      <Timeline items={cr.timeline} />
    </div>
  );
}

export function Deploys({ selected, onSelect }: { selected: number | null; onSelect: (id: number | null) => void }) {
  const list = useLoad(() => local.get<ChangeRequest[]>("/deploys"), [], 4000);
  const current = list.data?.find((c) => c.id === selected) ?? list.data?.[0];
  return (
    <div className="split">
      <Card title="My deploy requests">
        <ErrorBox error={list.error} />
        {list.data?.length === 0 && <Empty>No deploy requests yet.</Empty>}
        <ul className="list">
          {list.data?.map((cr) => (
            <li key={cr.id} className={current?.id === cr.id ? "active" : ""} onClick={() => onSelect(cr.id)}>
              <div className="list-main">
                <strong>#{cr.id}</strong> {cr.app}/{cr.component} → {cr.environment}
                <div className="muted small">{cr.source_branch} · {timeAgo(cr.created_at)}</div>
              </div>
              <Badge value={cr.status} />
            </li>
          ))}
        </ul>
      </Card>
      <Card title={current ? `#${current.id} ${current.app}/${current.component} → ${current.environment}` : "Details"}>
        {current ? <ChangeRequestDetail cr={current} /> : <Empty>Select a request.</Empty>}
      </Card>
    </div>
  );
}
