import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, followJob, type Job, type JobEvent, type JobResult } from "../api";
import { Markdown } from "./Markdown";
import { useVault } from "../vault";

function argSummary(args: unknown): string {
  if (!args || typeof args !== "object") return "";
  const a = args as Record<string, unknown>;
  const key = a.name ?? a.query ?? a.target ?? a.enzyme ?? a.protein ?? a.name_or_id ?? a.signor_id ?? a.source;
  return key ? String(key) : "";
}

export function JobProgress({ job, onFinished }: { job: Job; onFinished?: (job: Job) => void }) {
  const { vault, pages, refresh } = useVault();
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [status, setStatus] = useState(job.status);
  const [retrying, setRetrying] = useState<Job | null>(null);

  useEffect(() => {
    setEvents([]);
    setStatus(job.status);
    return followJob(job.id, (ev) => {
      setEvents((prev) => [...prev, ev]);
      if (ev.type === "done" || ev.type === "error") {
        const status = ev.type === "done" ? "succeeded" : "failed";
        setStatus(status);
        if (job.status === "queued" || job.status === "running") {
          refresh(); // the vault changed while we watched
          onFinished?.({ ...job, status });
        }
      } else {
        setStatus("running");
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);

  if (retrying) return <JobProgress job={retrying} onFinished={onFinished} />;

  const tools = events.filter((e) => e.type === "tool_call");
  const retries = events.filter((e) => e.type === "tool_result" && e.retry).length;
  const done = events.find((e) => e.type === "done");
  const error = events.find((e) => e.type === "error");
  const infos = events.filter((e) => e.type === "info");
  const result = done?.result as JobResult | undefined;
  const source = job.params?.source as string | undefined;

  return (
    <div className={`job job-${status}`}>
      <div className="job-head">
        <span className={`badge badge-${status}`}>{status}</span>
        <strong>{job.kind}</strong>
        {source && <Link to={`/${vault}/sources/${encodeURIComponent(source)}`} className="muted">{source}</Link>}
        <span className="muted">{tools.length} tool call{tools.length === 1 ? "" : "s"}{retries ? `, ${retries} corrected` : ""}</span>
      </div>
      {infos.map((e, i) => <div key={i} className="muted small">{String(e.message)}</div>)}
      {tools.length > 0 && (
        <ol className="steps">
          {tools.map((e, i) => (
            <li key={i}><code>{String(e.name)}</code> <span className="muted">{argSummary(e.args)}</span></li>
          ))}
          {status === "running" && <li className="muted pulse">working...</li>}
        </ol>
      )}
      {result && (
        <div className="job-result">
          {result.report && <Markdown text={result.report} vault={vault} pages={pages} />}
          <div className="chips">
            {result.pages_created.map((p) => (
              <Link key={p} className="chip chip-new" to={`/${vault}/wiki/${p}`}>+ {p}</Link>
            ))}
            {result.pages_updated.map((p) => (
              <Link key={p} className="chip" to={`/${vault}/wiki/${p}`}>~ {p}</Link>
            ))}
            {result.commit && <span className="chip chip-muted">commit {result.commit}</span>}
          </div>
          {result.warnings.map((w, i) => <div key={i} className="warning small">{w}</div>)}
        </div>
      )}
      {error && (
        <div className="error">
          {String(error.message)}
          {job.kind === "capture" && source && (
            <button className="btn btn-small" onClick={async () =>
              setRetrying(await api.startJob(vault, "capture", { source }))}>
              Retry
            </button>
          )}
        </div>
      )}
    </div>
  );
}
