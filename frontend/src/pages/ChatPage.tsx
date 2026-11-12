import { lazy, Suspense, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { api, chatApi, streamChat, type ConversationDetail, type ConversationSummary, type Job } from "../api";
import { CollapseButton, useRememberedToggle } from "../components/CollapseButton";
import { Markdown } from "../components/Markdown";
import { JobProgress } from "../components/JobProgress";
import { mergeNetworks } from "../components/networkUtils";
// Cytoscape is large: load it only when an answer has a network to draw.
const NetworkView = lazy(() => import("../components/NetworkView").then((m) => ({ default: m.NetworkView })));
import type { ChatContext, Network } from "../api";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
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
  graphs?: Network[]; // networks from graph tools, drawn under the answer
  context?: ChatContext; // what was looked up before the model answered
  notice?: string; // e.g. the answer was restarted after an invalid tool call
};
type Conversation = { session: string | null; messages: Msg[]; loading?: boolean; missing?: string };

// The open conversation per vault lives outside React so it survives navigating between tabs.
// Saved conversations come from the server (chatApi); the URL says which one is open.
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

/** A saved conversation as chat messages (the same fields the stream fills in). */
function toMessages(d: ConversationDetail): Msg[] {
  return d.turns.flatMap((t): Msg[] => [
    { role: "user", text: t.question, tools: [] },
    {
      role: "assistant", text: t.answer, tools: t.ui.tools ?? [], error: t.error ?? undefined,
      usage: t.ui.usage, graphs: t.ui.graphs, context: t.ui.context, notice: t.ui.notice,
    },
  ]);
}

const narrow = () => window.matchMedia?.("(max-width: 760px)").matches ?? false;

export function ChatPage() {
  const { vault, pages } = useVault();
  const { isAdmin } = useAuth();
  const conv = useSyncExternalStore(subscribe, () => getConv(vault));
  // "Ask in chat" from the Graph explorer pre-fills a question.
  const location = useLocation();
  const [input, setInput] = useState<string>((location.state as { prefill?: string } | null)?.prefill ?? "");
  const busy = conv.messages.some((m) => m.pending);
  const endRef = useRef<HTMLDivElement>(null);
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const [history, setHistory] = useState<ConversationSummary[]>([]);
  const [historyOpen, toggleHistory] = useRememberedToggle("chat.history.open", !narrow());
  const refreshHistory = useCallback(() => chatApi.list(vault).then(setHistory).catch(() => {}), [vault]);
  useEffect(() => { refreshHistory(); }, [refreshHistory]);

  // Keep the URL and the open conversation in step: /<vault>/chat shows the open one (if any),
  // /<vault>/chat/<id> loads that one from the server.
  useEffect(() => {
    const c = getConv(vault);
    if (!conversationId) {
      if (c.session) navigate(`/${vault}/chat/${c.session}`, { replace: true });
      return;
    }
    // A load still marked `loading` was cancelled (StrictMode runs effects twice on mount, and a
    // cancelled run ignores its result), so fetch again rather than wait on it.
    if ((conversationId === c.session && !c.loading) || c.messages.some((m) => m.pending)) return;
    let stale = false;
    setConv(vault, () => ({ session: conversationId, messages: [], loading: true }));
    chatApi.get(vault, conversationId)
      .then((d) => { if (!stale) setConv(vault, () => ({ session: d.id, messages: toMessages(d) })); })
      .catch((e) => {
        if (!stale) setConv(vault, () => ({ session: null, messages: [], missing: e instanceof Error ? e.message : String(e) }));
      });
    return () => { stale = true; };
  }, [vault, conversationId, navigate]);

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
        if (event === "session") {
          if (data.session_id !== getConv(vault).session) {
            setConv(vault, (c) => ({ ...c, session: data.session_id, missing: undefined }));
            navigate(`/${vault}/chat/${data.session_id}`, { replace: !conversationId });
          }
        }
        else if (event === "text") patchLast(vault, (m) => ({ ...m, text: m.text + data.delta }));
        else if (event === "tool_call")
          patchLast(vault, (m) => ({ ...m, tools: [...m.tools, { id: data.id, name: data.name, args: data.args }] }));
        else if (event === "tool_result")
          patchLast(vault, (m) => ({
            ...m,
            tools: m.tools.map((t) => (t.id === data.id ? { ...t, result: data.content, retry: data.retry } : t)),
          }));
        else if (event === "context") patchLast(vault, (m) => ({ ...m, context: data }));
        else if (event === "reset") patchLast(vault, (m) => ({ ...m, text: "", notice: data.message }));
        else if (event === "graph")
          patchLast(vault, (m) => ({ ...m, graphs: [...(m.graphs ?? []), { nodes: data.nodes, edges: data.edges }] }));
        else if (event === "done") patchLast(vault, (m) => ({ ...m, pending: false, usage: data.usage }));
        else if (event === "error") patchLast(vault, (m) => ({ ...m, pending: false, error: data.message }));
      });
    } catch (e) {
      patchLast(vault, (m) => ({ ...m, error: String(e) }));
    } finally {
      patchLast(vault, (m) => ({ ...m, pending: false }));
      refreshHistory();
    }
  }

  function newChat() {
    setConv(vault, () => ({ session: null, messages: [] }));
    navigate(`/${vault}/chat`);
  }

  function open(id: string) {
    if (narrow() && historyOpen) toggleHistory();
    navigate(`/${vault}/chat/${id}`);
  }

  const active = history.find((h) => h.id === conv.session);

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
    <div className={`chat-layout${historyOpen ? "" : " history-collapsed"}`}>
      <ChatHistory
        vault={vault} items={history} active={conv.session} disabled={busy} onOpen={open}
        onChanged={refreshHistory} onDeletedActive={newChat} open={historyOpen} onToggle={toggleHistory}
      />
    <div className="chat">
      <header className="page-head">
        <h1>Chat</h1>
        <span className="muted">Answers from your wiki and the PhosphoSIGNOR graph, with citations.</span>
        <button className="btn btn-ghost" onClick={newChat} disabled={busy || !conv.messages.length}>New chat</button>
      </header>

      <div className="messages">
        {conv.loading && <div className="empty muted pulse">Loading conversation...</div>}
        {conv.missing && <div className="error">This conversation can't be opened ({conv.missing}). Ask something to start a new one.</div>}
        {active && active.summarized_upto > 0 && (
          <div className="muted small summary-note">
            The model sees the earlier turns of this chat as a summary, and the latest ones in full.
          </div>
        )}
        {!conv.messages.length && !conv.loading && (
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
                {m.notice && <div className="muted small">{m.notice}</div>}
                {m.error && <div className="error">{m.error}</div>}
                {!m.pending && m.text && (
                  <div className="msg-actions">
                    {m.usage && <span className="muted small">{m.usage.requests} model calls · {m.usage.input_tokens + m.usage.output_tokens} tokens</span>}
                    {isAdmin && !m.saveJob && <button className="btn btn-small btn-ghost" onClick={() => save(i)}>Save to wiki</button>}
                  </div>
                )}
                {m.context && !m.pending && <Consulted vault={vault} context={m.context} />}
                {m.graphs && m.graphs.length > 0 && <AnswerGraph vault={vault} graphs={m.graphs} />}
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
        <button className="btn" disabled={busy || !input.trim() || conv.loading}>Send</button>
      </form>
    </div>
    </div>
  );
}

/** This vault's saved conversations, newest first, with rename and delete. */
function ChatHistory({ vault, items, active, disabled, onOpen, onChanged, onDeletedActive, open, onToggle }: {
  vault: string;
  items: ConversationSummary[];
  active: string | null;
  disabled: boolean;
  onOpen: (id: string) => void;
  onChanged: () => void;
  onDeletedActive: () => void;
  open: boolean;
  onToggle: () => void;
}) {
  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e));

  async function rename() {
    if (!editing) return;
    const { id, title } = editing;
    setEditing(null);
    if (!title.trim()) return;
    await chatApi.rename(vault, id, title).then(onChanged, fail);
  }

  async function remove(id: string) {
    setConfirming(null);
    await chatApi.remove(vault, id).then(() => {
      if (id === active) onDeletedActive();
      onChanged();
    }, fail);
  }

  if (!open) {
    return (
      <aside className="chat-history collapsed" aria-label="Past conversations">
        <CollapseButton collapsed onToggle={onToggle} label="past chats" />
        <span className="collapsed-label muted small">Past chats{items.length ? ` (${items.length})` : ""}</span>
      </aside>
    );
  }

  return (
    <aside className="chat-history" aria-label="Past conversations">
      <div className="chat-history-head muted small">
        <span>Past chats in this vault</span>
        <CollapseButton collapsed={false} onToggle={onToggle} label="past chats" />
      </div>
      {error && <div className="error small">{error}</div>}
      {!items.length && <div className="muted small">Your conversations will be listed here.</div>}
      <ul>
        {items.map((c) => (
          <li key={c.id} className={c.id === active ? "active" : undefined}>
            {editing?.id === c.id ? (
              <input
                autoFocus value={editing.title} maxLength={120} aria-label="Conversation title"
                onChange={(e) => setEditing({ id: c.id, title: e.target.value })}
                onBlur={rename}
                onKeyDown={(e) => {
                  if (e.key === "Enter") rename();
                  if (e.key === "Escape") setEditing(null);
                }}
              />
            ) : (
              <button className="chat-history-item" disabled={disabled} onClick={() => onOpen(c.id)} title={c.title}>
                <span className="chat-history-title">{c.title}</span>
                <span className="muted small">{new Date(c.updated_at).toLocaleDateString()} · {c.turns} turn{c.turns === 1 ? "" : "s"}</span>
              </button>
            )}
            {editing?.id !== c.id && (
              <span className="chat-history-actions">
                {confirming === c.id ? (
                  <>
                    <button className="btn btn-small btn-ghost btn-danger" onClick={() => remove(c.id)}>Delete</button>
                    <button className="btn btn-small btn-ghost" onClick={() => setConfirming(null)}>Keep</button>
                  </>
                ) : (
                  <>
                    <button className="icon-btn" title="Rename" aria-label="Rename" disabled={disabled}
                      onClick={() => setEditing({ id: c.id, title: c.title })}>✎</button>
                    <button className="icon-btn" title="Delete" aria-label="Delete" disabled={disabled}
                      onClick={() => setConfirming(c.id)}>×</button>
                  </>
                )}
              </span>
            )}
          </li>
        ))}
      </ul>
    </aside>
  );
}

/** The network behind an answer (all graph tool results merged), with a link to explore it. */
function AnswerGraph({ vault, graphs }: { vault: string; graphs: Network[] }) {
  const net = mergeNetworks(graphs);
  if (!net.edges.length) return null;
  const seeds = net.nodes.filter((n) => n.is_seed).map((n) => n.name);
  return (
    <div className="answer-graph">
      <div className="answer-graph-head muted small">
        {net.nodes.length} proteins · {net.edges.length} links
        {seeds.length > 0 && (
          <Link to={`/${vault}/graph?seeds=${encodeURIComponent(seeds.join(","))}`}>Open in Graph explorer</Link>
        )}
      </div>
      <Suspense fallback={<div className="muted small pulse">Drawing network...</div>}>
        <NetworkView data={net} height={280} compact />
      </Suspense>
    </div>
  );
}

/** What the answer was built from: wiki pages, recorded claims, graph proteins. */
function Consulted({ vault, context }: { vault: string; context: ChatContext }) {
  const parts = [
    context.pages.length ? `${context.pages.length} wiki page${context.pages.length > 1 ? "s" : ""}` : "no wiki pages",
    context.claims ? `${context.claims} claim${context.claims > 1 ? "s" : ""} from your sources` : null,
    context.graph_error ? "graph unavailable"
      : context.graph_entities.length ? `graph for ${context.graph_entities.join(", ")}` : null,
  ].filter(Boolean);
  return (
    <div className="consulted muted small">
      <span>Consulted: {parts.join(" · ")}</span>
      {context.pages.map((p) => <Link key={p.name} className="chip" to={`/${vault}/wiki/${p.name}`}>{p.title}</Link>)}
    </div>
  );
}
