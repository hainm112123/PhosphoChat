import { useEffect, useState } from "react";
import { api, type Job } from "../api";
import { JobProgress } from "../components/JobProgress";
import { useVault } from "../vault";

type Mode = "text" | "url" | "file";

export function CapturePage() {
  const { vault, version } = useVault();
  const [mode, setMode] = useState<Mode>("text");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [current, setCurrent] = useState<Job | null>(null);
  const [recent, setRecent] = useState<Job[]>([]);

  useEffect(() => {
    api.jobs(vault).then((js) => setRecent(js.filter((j) => j.kind === "capture"))).catch(() => {});
  }, [vault, version, current]);

  const ready = mode === "text" ? text.trim() : mode === "url" ? url.trim() : file;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!ready) return;
    const form = new FormData();
    if (mode === "text") form.set("text", text);
    if (mode === "url") form.set("url", url);
    if (mode === "file" && file) form.set("file", file);
    if (title.trim()) form.set("title", title.trim());
    setSubmitting(true);
    setError(null);
    try {
      const { job } = await api.capture(vault, form);
      setCurrent(job);
      setText(""); setUrl(""); setFile(null); setTitle("");
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <h1>Capture</h1>
        <span className="muted">Save one source unchanged, then let the agent fold it into the wiki.</span>
      </header>

      <form className="card capture" onSubmit={submit}>
        <div className="tabs">
          {(["text", "url", "file"] as Mode[]).map((m) => (
            <button type="button" key={m} className={`tab ${mode === m ? "active" : ""}`} onClick={() => setMode(m)}>
              {m === "text" ? "Paste text" : m === "url" ? "URL" : "File"}
            </button>
          ))}
        </div>
        {mode === "text" && (
          <textarea rows={12} value={text} onChange={(e) => setText(e.target.value)}
            placeholder="Paste an article, abstract or notes. The first line becomes the title unless you set one." />
        )}
        {mode === "url" && (
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://..." />
        )}
        {mode === "file" && (
          <label className="dropzone">
            <input type="file" accept=".md,.txt,.markdown,.csv,.tsv,.pdf"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            {file ? `${file.name} (${Math.ceil(file.size / 1024)} KB)` : "Choose a .md, .txt, .csv, .tsv or .pdf file"}
          </label>
        )}
        <div className="row">
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (optional)" />
          <button className="btn" disabled={!ready || submitting}>{submitting ? "Saving..." : "Capture"}</button>
        </div>
        {error && <div className="error">{error}</div>}
      </form>

      {current && <JobProgress key={current.id} job={current} />}

      {recent.filter((j) => j.id !== current?.id).length > 0 && (
        <section>
          <h2>Recent captures</h2>
          {recent.filter((j) => j.id !== current?.id).slice(0, 8).map((j) => (
            <details key={j.id} className="card compact">
              <summary>
                <span className={`badge badge-${j.status}`}>{j.status}</span>{" "}
                {String(j.params.source ?? "")} <span className="muted small">{j.created_at}</span>
              </summary>
              <JobProgress job={j} />
            </details>
          ))}
        </section>
      )}
    </div>
  );
}
