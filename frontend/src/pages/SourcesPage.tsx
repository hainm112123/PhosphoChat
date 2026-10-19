import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Source, type SourceSummary } from "../api";
import { Markdown } from "../components/Markdown";
import { useVault } from "../vault";

export function SourcesPage() {
  const { vault, pages, version } = useVault();
  const { name } = useParams();
  const [list, setList] = useState<SourceSummary[]>([]);
  const [source, setSource] = useState<Source | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { api.sources(vault).then(setList).catch(() => setList([])); }, [vault, version]);

  const isNote = name?.endsWith(".md") || name?.endsWith(".txt") || name?.endsWith(".tsv") || name?.endsWith(".csv");
  useEffect(() => {
    setError(null);
    setSource(null);
    if (name && isNote) api.source(vault, name).then(setSource).catch((e) => setError(e.message));
  }, [vault, name, isNote]);

  const meta = source?.meta ?? {};
  const original = meta.original_file as string | undefined;

  return (
    <div className="split">
      <aside className="list">
        <div className="muted small list-note">Sources are immutable: saved once, never edited.</div>
        {list.map((s) => (
          <Link key={s.name} to={`/${vault}/sources/${encodeURIComponent(s.name)}`}
            className={`list-item ${s.name === name ? "active" : ""}`}>
            <strong>{s.title ?? s.name}</strong>
            <span className="muted small">{s.content_type ?? s.name.split(".").pop()} · {s.name}</span>
          </Link>
        ))}
        {!list.length && <div className="muted small list-note">No sources yet.</div>}
      </aside>
      <article className="doc">
        {!name && <div className="empty"><p>Select a source.</p></div>}
        {error && <div className="error">{error}</div>}
        {name && !isNote && (
          <div className="empty">
            <p>{name} is a binary original.</p>
            <a className="btn" href={api.sourceUrl(vault, name)}>Download</a>
          </div>
        )}
        {source && (
          <>
            <header className="doc-head">
              <h1>{String(meta.title ?? source.name)}</h1>
              <div className="meta-row">
                {meta.content_type ? <span className="chip chip-muted">{String(meta.content_type)}</span> : null}
                {meta.ingested_at ? <span className="muted small">Ingested {String(meta.ingested_at)}</span> : null}
                {typeof meta.source_url === "string" && meta.source_url.startsWith("http") && (
                  <a href={meta.source_url} target="_blank" rel="noreferrer" className="small">Original page</a>
                )}
                {original && <a className="small" href={api.sourceUrl(vault, original)}>Download {original}</a>}
              </div>
              <div className="meta-row">
                <span className="muted small">Cited by:</span>
                {source.cited_by.length ? source.cited_by.map((p) => (
                  <Link key={p} className="chip" to={`/${vault}/wiki/${p}`}>{p}</Link>
                )) : <span className="muted small">no pages yet (run Sync or Retry the capture)</span>}
              </div>
            </header>
            {source.name.endsWith(".md")
              ? <Markdown text={source.body} vault={vault} pages={pages} />
              : <pre className="raw">{source.body}</pre>}
          </>
        )}
      </article>
    </div>
  );
}
