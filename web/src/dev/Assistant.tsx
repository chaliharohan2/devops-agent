import { useEffect, useRef, useState } from "react";
import { local } from "../api";
import type { ChatStep } from "../types";
import { Button, Card } from "../ui";

interface Msg {
  role: "user" | "agent";
  text: string;
  steps?: ChatStep[];
  source?: string;
  pending?: ChatStep | null;
}

interface ChatReply {
  session_id: string;
  reply: string;
  steps: ChatStep[];
  source?: string;
  pending?: ChatStep | null;
}

const SUGGESTIONS = [
  "Set up a test environment for my backend against dev",
  "What config changes does my branch introduce for staging?",
  "Deploy my pushed branch to dev",
  "What's the status of my deploys?",
];

export function Assistant() {
  const [messages, setMessages] = useState<Msg[]>([
    {
      role: "agent",
      text:
        "Hi! I'm your local DevOps agent. I can check what your pushed branch changes, ask the server agent to " +
        "deploy it, and build Docker test environments with your code plus everything else your app needs. " +
        "I never commit or push for you.",
    },
  ]);
  const [input, setInput] = useState("");
  const [session, setSession] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Block body on purpose: newer browsers return a Promise from smooth scrolling, and an
    // effect must return nothing or a cleanup function.
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send(text: string) {
    if (!text.trim() || busy) return;
    setMessages((m) => [...m, { role: "user", text }]);
    setInput("");
    setBusy(true);
    try {
      const res = await local.post<ChatReply>("/chat", { message: text, session_id: session });
      setSession(res.session_id);
      setMessages((m) => [
        ...m,
        { role: "agent", text: res.reply, steps: res.steps, source: res.source, pending: res.pending },
      ]);
    } catch (e) {
      setMessages((m) => [...m, { role: "agent", text: `⚠️ ${(e as Error).message}` }]);
    } finally {
      setBusy(false);
    }
  }

  const lastPending = [...messages].reverse().find((m) => m.role === "agent")?.pending
    || /\(yes\/no\)\s*$/.test([...messages].reverse().find((m) => m.role === "agent")?.text ?? "");

  return (
    <Card title="Assistant" className="chat-card">
      <div className="chat">
        {messages.map((m, i) => (
          <div key={i} className={`bubble bubble-${m.role}`}>
            <div className="bubble-text">{m.text}</div>
            {m.steps && m.steps.length > 0 && (
              <details className="steps">
                <summary>
                  {m.steps.length} tool call{m.steps.length > 1 ? "s" : ""}
                  {m.source ? ` · ${m.source}` : ""}
                </summary>
                {m.steps.map((s, j) => (
                  <div key={j} className="step">
                    <code>{s.tool}</code> <span className="muted small">{JSON.stringify(s.arguments)}</span>
                    {s.error ? <div className="bad small">{s.error}</div> : null}
                  </div>
                ))}
              </details>
            )}
          </div>
        ))}
        {busy && <div className="bubble bubble-agent muted">Thinking… (local model on CPU)</div>}
        <div ref={endRef} />
      </div>
      {lastPending ? (
        <div className="confirm-row">
          <span>The agent is waiting for your confirmation.</span>
          <Button kind="primary" onClick={() => send("yes")}>Yes, go ahead</Button>
          <Button onClick={() => send("no")}>No</Button>
        </div>
      ) : null}
      <div className="suggestions">
        {SUGGESTIONS.map((s) => (
          <button key={s} className="chip" onClick={() => send(s)} disabled={busy}>
            {s}
          </button>
        ))}
      </div>
      <form
        className="chat-input"
        onSubmit={(e) => {
          e.preventDefault();
          void send(input);
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask your agent… e.g. “spin up an e2e env for my frontend”"
          aria-label="Message"
        />
        <Button kind="primary" type="submit" disabled={busy || !input.trim()}>
          Send
        </Button>
      </form>
    </Card>
  );
}
