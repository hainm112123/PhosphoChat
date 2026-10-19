import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Page } from "../api";
import { Markdown } from "../components/Markdown";
import { useVault } from "../vault";

export function WikiPage() {
  const { vault, pageList, pages, version } = useVault();
  const { page: name } = useParams();
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState<Page | null>(null);
  const [index, setIndex] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    if (name) {
      api.page(vault, name).then(setPage).catch((e) => { setPage(null); setError(e.message); });
    } else {
      setPage(null);
      api.index(vault).then((r) => setIndex(r.text)).catch(() => setIndex(""));
    }
  }, [vault, name, version]);

  const shown = useMemo(() => {
    const q = filter.toLowerCase();
    return pageList.filter((p) => !q || p.name.includes(q) || p.title.toLowerCase().includes(q)
      || p.summary.toLowerCase().includes(q));
  }, [pageList, filter]);

  const meta = page?.meta ?? {};
  const sources = (meta.sources as string[] | undefined) ?? [];

  return (
    <div className="split">
      <aside className="list">
        <input placeholder={`Filter ${pageList.length} pages`} value={filter} onChange={(e) => setFilter(e.target.value)} />
        <Link to={`/${vault}/wiki`} className={`list-item ${!name ? "active" : ""}`}>
          <strong>Index</strong>
        </Link>
        {shown.map((p) => (
          <Link key={p.name} to={`/${vault}/wiki/${p.name}`} className={`list-item ${p.name === name ? "active" : ""}`}>
            <strong>{p.title}</strong>
            <span className="muted small">{p.summary}</span>
          </Link>
        ))}
      </aside>

      <article className="doc">
        {error && (
          <div className="empty">
            <p>{error}</p>
            <p className="muted">Pages appear here once a capture or sync job writes them.</p>
          </div>
        )}
        {!name && !error && (pageList.length === 0 ? (
          <div className="empty">
            <h2>Your wiki is empty</h2>
            <p>Capture a source and the agent will create the first pages.</p>
            <Link className="btn" to={`/${vault}/capture`}>Capture a source</Link>
          </div>
        ) : <Markdown text={index} vault={vault} pages={pages} />)}
        {page && (
          <>
            <header className="doc-head">
              <h1>{String(meta.title ?? page.name)}</h1>
              {meta.summary ? <p className="lede">{String(meta.summary)}</p> : null}
              <div className="meta-row">
                {meta.last_updated ? <span className="muted small">Updated {String(meta.last_updated)}</span> : null}
                {sources.map((s) => (
                  <Link key={s} className="chip" to={`/${vault}/sources/${encodeURIComponent(s)}`}>{s}</Link>
                ))}
              </div>
            </header>
            <Markdown text={page.body} vault={vault} pages={pages} />
            <footer className="backlinks">
              <h3>Linked from</h3>
              {page.backlinks.length ? page.backlinks.map((b) => (
                <Link key={b} className="chip" to={`/${vault}/wiki/${b}`}>{b}</Link>
              )) : <span className="muted small">No incoming links (orphan). Lint will flag this.</span>}
            </footer>
          </>
        )}
      </article>
    </div>
  );
}
