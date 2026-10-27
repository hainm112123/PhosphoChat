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
  // Starts at the server default (VERIFY_CAPTURES); a choice made here applies per capture.
  const [verify, setVerify] = useState(false);
  const [verifyModel, setVerifyModel] = useState("");

  useEffect(() => {
    api.config().then((c) => {
      setVerify(c.verify_captures);
      setVerifyModel(c.models.verify);
    }).catch(() => {});
  }, []);

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
    form.set("verify", String(verify));
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
        <span className="muted">The agent folds one source into the wiki. If any step fails, nothing is saved.</span>
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
        <div className="option-row">
          <label className="switch" htmlFor="capture-verify">
            <input id="capture-verify" type="checkbox" role="switch" checked={verify}
              onChange={(e) => setVerify(e.target.checked)} />
            <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
            <span>Fact-check against the source</span>
          </label>
          <span className="help">
            <button type="button" className="help-icon" aria-label="What is the fact-check?"
              aria-describedby="fact-check-help">?</button>
            <span id="fact-check-help" role="tooltip" className="help-tip">
              After the agent writes the wiki pages, a second model
              {verifyModel ? <> (<code>{verifyModel}</code>)</> : null} compares every claim with
              the source text. Claims the source doesn't support, such as added mechanisms,
              residues or numbers, go back to the agent once to be fixed or removed.
              <br /><br />
              It roughly doubles the tokens a capture uses. If the check itself fails (for
              example a rate limit), the whole capture fails and nothing is saved. Turn it off
              to save tokens; every paragraph still has to cite a source.
            </span>
          </span>
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
                {String(j.params.title ?? j.params.source ?? "")} <span className="muted small">{j.created_at}</span>
              </summary>
              <JobProgress job={j} />
            </details>
          ))}
        </section>
      )}
    </div>
  );
}
