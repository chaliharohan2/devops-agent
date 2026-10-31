import { useEffect, useState } from "react";
import { getToken, local, probe, server, setToken, setServerToken } from "./api";
import { Assistant } from "./dev/Assistant";
import { Deploy } from "./dev/Deploy";
import { Deploys } from "./dev/Deploys";
import { Envs } from "./dev/Envs";
import { Peers } from "./dev/Peers";
import { Setup } from "./dev/Setup";
import { ChangeRequests, Deployments } from "./server/Changes";
import { Configs, ServerChat } from "./server/Configs";
import { Audit, Login, Overview } from "./server/Overview";
import { Spinner } from "./ui";

type Mode = "developer" | "server";

const DEV_PAGES = [
  { id: "assistant", label: "Assistant", icon: "✦" },
  { id: "deploy", label: "Deploy", icon: "⇪" },
  { id: "deploys", label: "My deploys", icon: "☰" },
  { id: "envs", label: "Test environments", icon: "▣" },
  { id: "peers", label: "Teammates", icon: "⇄" },
  { id: "setup", label: "Setup", icon: "⚙" },
];

const SERVER_PAGES = [
  { id: "overview", label: "Overview", icon: "◉" },
  { id: "changes", label: "Change requests", icon: "⇪" },
  { id: "deployments", label: "Deployments", icon: "▶" },
  { id: "configs", label: "Configuration", icon: "⚙" },
  { id: "audit", label: "Audit log", icon: "☰" },
  { id: "chat", label: "Ask the agent", icon: "✦" },
];

function parseHash(): { page: string; id: number | null } {
  const [, page = "", id] = window.location.hash.split("/");
  return { page, id: id ? Number(id) : null };
}

export function App() {
  const [mode, setMode] = useState<Mode | null>(null);
  const [both, setBoth] = useState(false);
  const [route, setRoute] = useState(parseHash());
  const [me, setMe] = useState<{ username: string; role: string } | null>(null);
  const [devUser, setDevUser] = useState<string | null>(null);
  const [token, setTok] = useState<string | null>(getToken());

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    (async () => {
      const forced = new URLSearchParams(window.location.search).get("mode") as Mode | null;
      const [isLocal, isServer] = await Promise.all([probe("/api/ping"), probe("/api/v1/health")]);
      setBoth(isLocal && isServer);
      setMode(forced ?? (isLocal ? "developer" : "server"));
    })();
  }, []);

  useEffect(() => {
    if (mode === "developer") {
      local.get<{ username: string | null }>("/status").then((s) => setDevUser(s.username)).catch(() => undefined);
    }
    if (mode === "server" && token) {
      setServerToken(token);
      server.get<{ user: { username: string; role: string } }>("/me")
        .then((r) => setMe(r.user))
        .catch(() => { setToken(null); setTok(null); });
    }
  }, [mode, token]);

  const go = (page: string, id?: number) => {
    window.location.hash = `/${page}${id ? `/${id}` : ""}`;
  };

  if (mode === null) {
    return <div className="boot"><Spinner /> Connecting to your agents…</div>;
  }
  if (mode === "server" && !token) {
    return <Login onLogin={(t) => { setTok(t); }} />;
  }

  const pages = mode === "developer" ? DEV_PAGES : SERVER_PAGES;
  const page = pages.some((p) => p.id === route.page) ? route.page : pages[0].id;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">◆</span>
          <div>
            <div className="brand-name">DevOps Agent</div>
            <div className="brand-sub">{mode === "developer" ? "Developer app" : "Server dashboard"}</div>
          </div>
        </div>
        <nav>
          {pages.map((p) => (
            <a key={p.id} href={`#/${p.id}`} className={page === p.id ? "active" : ""}>
              <span className="nav-icon">{p.icon}</span>{p.label}
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          {mode === "developer" ? (
            <div className="muted small">Signed in as <strong>{devUser ?? "—"}</strong></div>
          ) : (
            <div className="muted small">
              {me ? <>Signed in as <strong>{me.username}</strong> ({me.role})</> : null}
              <button className="link" onClick={() => { setToken(null); setTok(null); setMe(null); }}>Sign out</button>
            </div>
          )}
          {both && (
            <button className="link" onClick={() => { setMode(mode === "developer" ? "server" : "developer"); window.location.hash = ""; }}>
              Switch to {mode === "developer" ? "server dashboard" : "developer app"}
            </button>
          )}
        </div>
      </aside>
      <main className="main">
        <header className="page-head">
          <h1>{pages.find((p) => p.id === page)?.label}</h1>
        </header>
        {mode === "developer" ? (
          <>
            {page === "assistant" && <Assistant />}
            {page === "deploy" && <Deploy onOpenRequest={(id) => go("deploys", id)} />}
            {page === "deploys" && <Deploys selected={route.id} onSelect={(id) => go("deploys", id ?? undefined)} />}
            {page === "envs" && <Envs />}
            {page === "peers" && <Peers me={devUser} />}
            {page === "setup" && <Setup />}
          </>
        ) : (
          <>
            {page === "overview" && <Overview go={go} />}
            {page === "changes" && <ChangeRequests selected={route.id} onSelect={(id) => go("changes", id)} role={me?.role ?? "developer"} />}
            {page === "deployments" && <Deployments selected={route.id} onSelect={(id) => go("deployments", id)} />}
            {page === "configs" && <Configs role={me?.role ?? "developer"} />}
            {page === "audit" && <Audit />}
            {page === "chat" && <ServerChat />}
          </>
        )}
      </main>
    </div>
  );
}
