// Thin client for the PhosphoChat API.

export type PageSummary = {
  name: string;
  title: string;
  summary: string;
  last_updated: string | null;
  sources: string[];
};
export type Page = { name: string; meta: Record<string, unknown>; body: string; backlinks: string[] };
export type SourceSummary = {
  name: string;
  size: number;
  title?: string | null;
  content_type?: string | null;
  ingested_at?: string | null;
  source_url?: string | null;
};
export type Source = { name: string; meta: Record<string, unknown>; body: string; cited_by: string[] };
export type LogEntry = { timestamp: string; kind: string; body: string };
export type JobEvent = { type: string; [k: string]: unknown };
export type FactIssue = {
  page: string;
  claim: string;
  problem: "not_in_source" | "contradicts_source" | "wrong_citation";
  note: string;
};
export type FactCheck = { checked_pages: string[]; issues_found: number; issues: FactIssue[] };
export type JobResult = {
  report: string;
  pages_created: string[];
  pages_updated: string[];
  warnings: string[];
  commit: string | null;
  fact_check?: FactCheck | null;
};
export type Job = {
  id: string;
  vault: string;
  kind: string;
  params: Record<string, unknown>;
  status: "queued" | "running" | "succeeded" | "failed";
  created_at: string;
  finished_at: string | null;
  result: JobResult | null;
  error: string | null;
  retryable?: boolean;
};

export type Role = "user" | "admin";
export type User = { id: number; username: string; role: Role; disabled: boolean; created_at: string };

/** Fired when the server says the session is gone (expired, logged out elsewhere, disabled). */
export const UNAUTHORIZED_EVENT = "phosphochat:unauthorized";

export class HttpError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new HttpError(detail, res.status);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const authApi = {
  status: () => request<{ needs_admin: boolean }>("/api/auth/status"),
  me: () => request<User>("/api/auth/me"),
  login: (username: string, password: string) => request<User>("/api/auth/login", json("POST", { username, password })),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  changePassword: (current: string, next: string) =>
    request<void>("/api/auth/password", json("POST", { current, new: next })),
  users: () => request<User[]>("/api/users"),
  createUser: (username: string, password: string, role: Role) =>
    request<User>("/api/users", json("POST", { username, password, role })),
  updateUser: (id: number, patch: { role?: Role; password?: string; disabled?: boolean }) =>
    request<User>(`/api/users/${id}`, json("PATCH", patch)),
  deleteUser: (id: number) => request<void>(`/api/users/${id}`, { method: "DELETE" }),
};

const v = (vault: string) => `/api/vaults/${encodeURIComponent(vault)}`;

export const api = {
  config: () => request<{
    model: string;
    models: { chat: string; job: string; verify: string };
    default_vault: string;
    verify_captures: boolean;
  }>("/api/config"),
  health: () => request<{ graph: { ok: boolean; error?: string } }>("/api/health"),
  vaults: () => request<{ name: string; pages: number; sources: number }[]>("/api/vaults"),
  createVault: (name: string) =>
    request<{ name: string }>("/api/vaults", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  pages: (vault: string) => request<PageSummary[]>(`${v(vault)}/pages`),
  page: (vault: string, name: string) => request<Page>(`${v(vault)}/pages/${encodeURIComponent(name)}`),
  sources: (vault: string) => request<SourceSummary[]>(`${v(vault)}/sources`),
  source: (vault: string, name: string) => request<Source>(`${v(vault)}/sources/${encodeURIComponent(name)}`),
  sourceUrl: (vault: string, name: string) => `${v(vault)}/sources/${encodeURIComponent(name)}`,
  index: (vault: string) => request<{ text: string; entries: Record<string, string> }>(`${v(vault)}/index`),
  log: (vault: string) => request<{ text: string; entries: LogEntry[] }>(`${v(vault)}/log`),
  jobs: (vault: string) => request<Job[]>(`${v(vault)}/jobs`),
  job: (id: string) => request<Job & { events: JobEvent[] }>(`/api/jobs/${id}`),
  capture: (vault: string, form: FormData) =>
    request<{ job: Job }>(`${v(vault)}/capture`, { method: "POST", body: form }),
  retryJob: (id: string) => request<Job>(`/api/jobs/${id}/retry`, { method: "POST" }),
  startJob: (vault: string, kind: string, options: Record<string, unknown> = {}) =>
    request<Job>(`${v(vault)}/jobs/${kind}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options),
    }),
  saveAnswer: (vault: string, question: string, answer: string) =>
    request<{ job: Job }>(`${v(vault)}/chat/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, answer }),
    }),
  resetChat: (vault: string, session: string) =>
    request<void>(`${v(vault)}/chat/${session}`, { method: "DELETE" }),
};

/** POST a chat message and call onEvent for every server-sent event. */
export async function streamChat(
  vault: string,
  body: { message: string; session_id?: string | null },
  onEvent: (event: string, data: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${v(vault)}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!res.ok || !res.body) throw new Error(`${res.status} ${res.statusText}`);
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replace(/\r\n/g, "\n");
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (data.length) {
        try {
          onEvent(event, JSON.parse(data.join("\n")));
        } catch {
          onEvent(event, data.join("\n"));
        }
      }
    }
  }
}

/** Follow a job's progress events (replays from the start). Returns a close function. */
export function followJob(id: string, onEvent: (e: JobEvent) => void): () => void {
  const es = new EventSource(`/api/jobs/${id}/events`);
  const handler = (msg: MessageEvent) => {
    const ev = JSON.parse(msg.data) as JobEvent;
    onEvent(ev);
    if (ev.type === "done" || ev.type === "error") es.close();
  };
  for (const type of ["info", "text", "tool_call", "tool_result", "fact_check", "usage", "done", "error"]) {
    es.addEventListener(type, handler as EventListener);
  }
  es.onerror = () => es.close();
  return () => es.close();
}
