import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

export function Card(props: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${props.className ?? ""}`}>
      {(props.title || props.actions) && (
        <header className="card-head">
          <h2>{props.title}</h2>
          <div className="card-actions">{props.actions}</div>
        </header>
      )}
      {props.children}
    </section>
  );
}

const STATUS_TONE: Record<string, string> = {
  deployed: "ok", succeeded: "ok", running: "ok", healthy: "ok", approved: "ok", uploaded: "ok", fetched: "ok",
  merged: "info", merging: "info", deploying: "info", analyzing: "info", received: "info", building: "info",
  preparing: "info", pending: "warn", awaiting_approval: "warn", starting: "warn", medium: "warn",
  failed: "bad", conflict: "bad", rejected: "bad", denied: "bad", unhealthy: "bad", high: "bad",
  stopped: "muted", expired: "muted", low: "ok",
};

export function Badge({ value, tone }: { value: string; tone?: string }) {
  const t = tone ?? STATUS_TONE[value] ?? "muted";
  return <span className={`badge badge-${t}`}>{value.replace(/_/g, " ")}</span>;
}

export function Button(props: {
  children: ReactNode;
  onClick?: () => void | Promise<void>;
  kind?: "primary" | "secondary" | "danger" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
  small?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type={props.type ?? "button"}
      className={`btn btn-${props.kind ?? "secondary"} ${props.small ? "btn-sm" : ""}`}
      disabled={props.disabled || busy}
      onClick={async () => {
        if (!props.onClick) return;
        setBusy(true);
        try {
          await props.onClick();
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? <Spinner /> : null}
      {props.children}
    </button>
  );
}

export function Spinner() {
  return <span className="spinner" aria-label="loading" />;
}

export function Field(props: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{props.label}</span>
      {props.children}
      {props.hint ? <span className="field-hint">{props.hint}</span> : null}
    </label>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="alert alert-bad">{error instanceof Error ? error.message : String(error)}</div>;
}

export function Alert({ tone, children }: { tone: "ok" | "warn" | "bad" | "info"; children: ReactNode }) {
  return <div className={`alert alert-${tone}`}>{children}</div>;
}

export function Pre({ children, maxHeight }: { children: ReactNode; maxHeight?: number }) {
  return (
    <pre className="pre" style={maxHeight ? { maxHeight } : undefined}>
      {children}
    </pre>
  );
}

export function Sha({ sha }: { sha?: string | null }) {
  if (!sha) return <span className="muted">—</span>;
  return (
    <code className="sha" title={sha}>
      {sha.slice(0, 10)}
    </code>
  );
}

export function timeAgo(iso?: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

/** Load data, optionally re-polling every `intervalMs`. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = [], intervalMs?: number) {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const reload = useCallback(async () => {
    try {
      const d = await loaderRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    setLoading(true);
    void reload();
    if (!intervalMs) return;
    const t = setInterval(() => void reload(), intervalMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload, setData };
}

export function Timeline({ items }: { items: { at: string; status: string; message: string }[] }) {
  return (
    <ol className="timeline">
      {items.map((t, i) => (
        <li key={i}>
          <span className="timeline-dot" data-tone={STATUS_TONE[t.status] ?? "muted"} />
          <div>
            <div className="timeline-head">
              <Badge value={t.status} />
              <span className="muted small">{t.at ? new Date(t.at).toLocaleTimeString() : ""}</span>
            </div>
            {t.message ? <div className="small">{t.message}</div> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}
