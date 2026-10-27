import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api, streamChat, type Job } from "../api";
import { Markdown } from "../components/Markdown";
import { JobProgress } from "../components/JobProgress";
import { useAuth } from "../auth";
import { useVault } from "../vault";

type ToolCall = { id: string; name: string; args: unknown; result?: string; retry?: boolean };
type Msg = {
  role: "user" | "assistant";
  text: string;
  tools: ToolCall[];
  error?: string;
  pending?: boolean;
  usage?: { requests: number; input_tokens: number; output_tokens: number };
  saveJob?: Job;
  saveError?: string;
};
type Conversation = { session: string | null; messages: Msg[] };

// Conversations live outside React so they survive navigating between tabs.
const store = new Map<string, Conversation>();
const listeners = new Set<() => void>();
function getConv(vault: string): Conversation {
  // useSyncExternalStore needs a stable snapshot, so create the entry once.
  if (!store.has(vault)) store.set(vault, { session: null, messages: [] });
  return store.get(vault)!;
}
function setConv(vault: string, update: (c: Conversation) => Conversation) {
  store.set(vault, update(getConv(vault)));
  listeners.forEach((l) => l());
}
const subscribe = (l: () => void) => (listeners.add(l), () => listeners.delete(l));

const EXAMPLES = [
  "Which kinases phosphorylate ARX?",
  "What does PTPN1 dephosphorylate?",
  "How could EGFR signalling reach MYC?",
  "What is in my wiki so far?",
];

function patchLast(vault: string, fn: (m: Msg) => Msg) {
  setConv(vault, (c) => ({ ...c, messages: [...c.messages.slice(0, -1), fn(c.messages[c.messages.length - 1])] }));
}

export function ChatPage() {
  const { vault, pages } = useVault();
  const { isAdmin } = useAuth();
  const conv = useSyncExternalStore(subscribe, () => getConv(vault));
  const [input, setInput] = useState("");
  const busy = conv.messages.some((m) => m.pending);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Braces matter: newer Chromium returns a Promise from scrollIntoView, and an effect
    // must return nothing or a cleanup function.
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [conv.messages]);

  async function send(text: string) {
    const message = text.trim();
    if (!message || busy) return;
    setInput("");
    setConv(vault, (c) => ({
      ...c,
      messages: [...c.messages, { role: "user", text: message, tools: [] },
        { role: "assistant", text: "", tools: [], pending: true }],
    }));
    try {
      await streamChat(vault, { message, session_id: getConv(vault).session }, (event, data) => {
        if (event === "session") setConv(vault, (c) => ({ ...c, session: data.session_id }));
        else if (event === "text") patchLast(vault, (m) => ({ ...m, text: m.text + data.delta }));
        else if (event === "tool_call")
          patchLast(vault, (m) => ({ ...m, tools: [...m.tools, { id: data.id, name: data.name, args: data.args }] }));
        else if (event === "tool_result")
          patchLast(vault, (m) => ({
            ...m,
            tools: m.tools.map((t) => (t.id === data.id ? { ...t, result: data.content, retry: data.retry } : t)),
          }));
        else if (event === "done") patchLast(vault, (m) => ({ ...m, pending: false, usage: data.usage }));
        else if (event === "error") patchLast(vault, (m) => ({ ...m, pending: false, error: data.message }));
      });
    } catch (e) {
      patchLast(vault, (m) => ({ ...m, error: String(e) }));
    } finally {
      patchLast(vault, (m) => ({ ...m, pending: false }));
    }
  }

  async function newChat() {
    if (conv.session) await api.resetChat(vault, conv.session).catch(() => {});
    setConv(vault, () => ({ session: null, messages: [] }));
  }

  async function save(i: number) {
    const question = conv.messages[i - 1]?.text ?? "";
    try {
      const { job } = await api.saveAnswer(vault, question, conv.messages[i].text);
      setConv(vault, (c) => ({ ...c, messages: c.messages.map((m, j) => (j === i ? { ...m, saveJob: job } : m)) }));
    } catch (e) {
      const saveError = e instanceof Error ? e.message : String(e);
      setConv(vault, (c) => ({ ...c, messages: c.messages.map((m, j) => (j === i ? { ...m, saveError } : m)) }));
    }
  }

  return (
    <div className="chat">
      <header className="page-head">
        <h1>Chat</h1>
        <span className="muted">Answers from your wiki and the PhosphoSIGNOR graph, with citations.</span>
        <button className="btn btn-ghost" onClick={newChat} disabled={busy || !conv.messages.length}>New chat</button>
      </header>

      <div className="messages">
        {!conv.messages.length && (
          <div className="empty">
            <p>Ask about kinases, substrates, phosphosites and pathways, or about anything you've captured.</p>
            <div className="examples">
              {EXAMPLES.map((q) => <button key={q} className="chip chip-btn" onClick={() => send(q)}>{q}</button>)}
            </div>
          </div>
        )}
        {conv.messages.map((m, i) => (
          <div key={i} className={`msg msg-${m.role}`}>
            {m.role === "user" ? <div className="bubble">{m.text}</div> : (
              <div className="answer">
                {m.tools.length > 0 && (
                  <details className="trace">
                    <summary>
                      {m.tools.length} tool call{m.tools.length > 1 ? "s" : ""}:{" "}
                      {m.tools.map((t) => t.name).join(", ")}
                    </summary>
                    {m.tools.map((t) => (
                      <div key={t.id} className="trace-item">
                        <div><code>{t.name}</code> <span className="muted">{JSON.stringify(t.args)}</span>
                          {t.retry && <span className="badge badge-failed">retry</span>}</div>
                        {t.result && <pre>{t.result}</pre>}
                      </div>
                    ))}
                  </details>
                )}
                {m.text ? <Markdown text={m.text} vault={vault} pages={pages} />
                  : m.pending && <div className="muted pulse">{m.tools.length ? "Reading results..." : "Thinking..."}</div>}
                {m.error && <div className="error">{m.error}</div>}
                {!m.pending && m.text && (
                  <div className="msg-actions">
                    {m.usage && <span className="muted small">{m.usage.requests} model calls · {m.usage.input_tokens + m.usage.output_tokens} tokens</span>}
                    {isAdmin && !m.saveJob && <button className="btn btn-small btn-ghost" onClick={() => save(i)}>Save to wiki</button>}
                  </div>
                )}
                {m.saveError && <div className="error">Couldn't save to the wiki: {m.saveError}</div>}
                {m.saveJob && <JobProgress job={m.saveJob} />}
              </div>
            )}
          </div>
        ))}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <textarea
          value={input}
          placeholder="Ask a question... (Enter to send, Shift+Enter for a new line)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); }
          }}
          rows={2}
        />
        <button className="btn" disabled={busy || !input.trim()}>Send</button>
      </form>
    </div>
  );
}
