import { useEffect, useState } from "react";
import { api, type Job, type LogEntry } from "../api";
import { JobProgress } from "../components/JobProgress";
import { Markdown } from "../components/Markdown";
import { useAuth } from "../auth";
import { useVault } from "../vault";

const JOBS = [
  { kind: "sync", label: "Sync", help: "Process sources added outside Capture" },
  { kind: "lint", label: "Lint", help: "Health-check links, orphans, contradictions" },
  { kind: "digest", label: "Digest", help: "Write this week's recap" },
];

export function LogPage() {
  const { vault, pages, version } = useVault();
  const { isAdmin } = useAuth();
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [current, setCurrent] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.log(vault).then((r) => setEntries(r.entries)).catch(() => setEntries([]));
    if (isAdmin) api.jobs(vault).then(setJobs).catch(() => setJobs([]));
  }, [vault, version, current, isAdmin]);

  async function start(kind: string) {
    setError(null);
    try {
      setCurrent(await api.startJob(vault, kind));
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  }

  const running = jobs.some((j) => j.status === "running" || j.status === "queued");

  return (
    <div className="page">
      <header className="page-head">
        <h1>Log</h1>
        <span className="muted">Every session that changed the vault, newest first.</span>
      </header>

      {isAdmin && (
        <div className="job-buttons">
          {JOBS.map((j) => (
            <button key={j.kind} className="btn btn-ghost" title={j.help} onClick={() => start(j.kind)}>
              {j.label}
              <span className="muted small"> {j.help}</span>
            </button>
          ))}
        </div>
      )}
      {running && !current && <div className="muted small">A job is running on this vault; new jobs wait their turn.</div>}
      {error && <div className="error">{error}</div>}
      {current && <JobProgress key={current.id} job={current} />}

      <section className="log">
        {!entries.length && <div className="empty"><p>No sessions yet.</p></div>}
        {entries.map((e, i) => (
          <div key={i} className="card log-entry">
            <div className="log-head">
              <span className={`badge badge-kind-${e.kind}`}>{e.kind}</span>
              <span className="muted small">{e.timestamp}</span>
            </div>
            <Markdown text={e.body} vault={vault} pages={pages} />
          </div>
        ))}
      </section>
    </div>
  );
}
