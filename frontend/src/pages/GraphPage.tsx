import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { graphApi, type Interaction, type NetEdge, type NetNode, type Network, type NetworkQuery } from "../api";
import { GroupLegend, NetworkView, nodeColor, type NetworkHandle } from "../components/NetworkView";
import { useVault } from "../vault";

const ALL_SOURCES = ["SIGNOR", "PhosphoSitePlus", "EPSD", "iPTMNet"];

function download(name: string, href: string) {
  const a = document.createElement("a");
  a.href = href;
  a.download = name;
  a.click();
}

export function GraphPage() {
  const { vault } = useVault();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const seeds = useMemo(() => (params.get("seeds") ?? "").split(",").map((s) => s.trim()).filter(Boolean), [params]);

  const [depth, setDepth] = useState(1);
  const [direction, setDirection] = useState<NetworkQuery["direction"]>("both");
  const [sources, setSources] = useState<string[]>(ALL_SOURCES);
  const [minScore, setMinScore] = useState(0);
  const [minSources, setMinSources] = useState(1);
  const [mechanism, setMechanism] = useState("");
  const [groups, setGroups] = useState<string[]>([]);

  const [net, setNet] = useState<Network | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ node?: NetNode; edge?: NetEdge } | null>(null);
  const view = useRef<NetworkHandle>(null);

  useEffect(() => { graphApi.meta().then((m) => setGroups(m.kinase_groups)).catch(() => {}); }, []);

  useEffect(() => {
    if (!seeds.length) { setNet(null); return; }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const t = setTimeout(() => {
      graphApi.network({ seeds, depth, direction, sources: sources.length === ALL_SOURCES.length ? [] : sources,
                         min_score: minScore, min_sources: minSources, mechanism, limit: 300 })
        .then((n) => { if (!cancelled) { setNet(n); setSelected(null); } })
        .catch((e) => { if (!cancelled) setError(e.message); })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, 250);
    return () => { cancelled = true; clearTimeout(t); };
  }, [seeds, depth, direction, sources, minScore, minSources, mechanism]);

  const setSeeds = (next: string[]) => {
    const p = new URLSearchParams(params);
    if (next.length) p.set("seeds", [...new Set(next)].join(",")); else p.delete("seeds");
    setParams(p, { replace: false });
  };
  const byId = useMemo(() => new Map((net?.nodes ?? []).map((n) => [n.id, n])), [net]);

  function exportCsv() {
    if (!net) return;
    const rows = [["enzyme", "enzyme_uniprot", "target", "target_uniprot", "mechanism", "sites", "databases", "signor_id", "signor_score", "effect", "pmid_count"]];
    for (const e of net.edges) {
      rows.push([byId.get(e.source)?.name ?? e.source, e.source, byId.get(e.target)?.name ?? e.target, e.target,
                 e.mechanism, e.sites.join(";"), e.sources.join(";"), e.signor_id ?? "", e.score?.toString() ?? "",
                 e.effect ?? "", String(e.n_pmids)]);
    }
    const csv = rows.map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n");
    download(`phosphochat-${seeds.join("_")}.csv`, URL.createObjectURL(new Blob([csv], { type: "text/csv" })));
  }

  return (
    <div className="graph-page">
      <aside className="graph-side">
        <h1>Graph</h1>
        <p className="muted small">Kinase–substrate links from PhosphoSIGNOR and KiNet (PhosphoSitePlus, EPSD, iPTMNet).</p>
        <SeedPicker seeds={seeds} onChange={setSeeds} />

        <fieldset>
          <legend>Show</legend>
          <div className="seg">
            {(["regulators", "both", "targets"] as const).map((d) => (
              <button key={d} type="button" className={direction === d ? "active" : ""} onClick={() => setDirection(d)}>
                {d === "regulators" ? "Regulators" : d === "targets" ? "Targets" : "Both"}
              </button>
            ))}
          </div>
          <label className="row-label">Depth
            <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
              <option value={1}>1 step</option><option value={2}>2 steps</option>
            </select>
          </label>
          <label className="row-label">Mechanism
            <select value={mechanism} onChange={(e) => setMechanism(e.target.value)}>
              <option value="">Both</option>
              <option value="phosphorylation">Phosphorylation</option>
              <option value="dephosphorylation">Dephosphorylation</option>
            </select>
          </label>
        </fieldset>

        <fieldset>
          <legend>Databases</legend>
          {ALL_SOURCES.map((s) => (
            <label key={s} className="check">
              <input type="checkbox" checked={sources.includes(s)}
                onChange={(e) => setSources(e.target.checked ? [...sources, s] : sources.filter((x) => x !== s))} />
              {s}
            </label>
          ))}
          <label className="row-label">Reported by at least
            <select value={minSources} onChange={(e) => setMinSources(Number(e.target.value))}>
              {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n} database{n > 1 ? "s" : ""}</option>)}
            </select>
          </label>
          <label className="row-label" htmlFor="min-score">SIGNOR score ≥ {minScore.toFixed(1)}</label>
          <input id="min-score" type="range" min={0} max={1} step={0.1} value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))} />
          {minScore > 0 && <div className="muted small">Hides links only KiNet databases report (they have no score).</div>}
        </fieldset>

        <fieldset>
          <legend>Kinase groups</legend>
          <GroupLegend groups={groups} />
        </fieldset>

        <div className="export">
          <button className="btn btn-small btn-ghost" disabled={!net?.edges.length} onClick={exportCsv}>Export CSV</button>
          <button className="btn btn-small btn-ghost" disabled={!net?.edges.length}
            onClick={() => { const png = view.current?.png(); if (png) download(`phosphochat-${seeds.join("_")}.png`, png); }}>
            Export PNG
          </button>
        </div>
      </aside>

      <section className="graph-main">
        {!seeds.length && (
          <div className="empty">
            <h2>Explore kinase–substrate networks</h2>
            <p>Add one or more proteins on the left, for example
              {" "}{["TDP1", "TP53", "ATM", "MAPK1"].map((s, i) => (
                <span key={s}>{i ? ", " : ""}<button className="link-btn" onClick={() => setSeeds([s])}>{s}</button></span>
              ))}.</p>
          </div>
        )}
        {error && <div className="error">{error}</div>}
        {seeds.length > 0 && net && (
          <>
            <div className="graph-status muted small">
              {loading ? "Updating..." : `${net.nodes.length} proteins · ${net.edges.length} links`}
              {net.truncated && " · showing the 300 best-supported links; narrow the filters to see others"}
              <button className="link-btn" onClick={() => view.current?.fit()}>Fit</button>
            </div>
            {net.edges.length === 0
              ? <div className="empty"><p>No links match these filters.</p></div>
              : <NetworkView ref={view} data={net} height="calc(100vh - 140px)"
                  onNode={(node) => setSelected({ node })} onEdge={(edge) => setSelected({ edge })} />}
          </>
        )}
        {seeds.length > 0 && !net && loading && <div className="empty muted pulse">Loading network...</div>}
      </section>

      {selected && (
        <aside className="graph-detail">
          <button className="link-btn close" onClick={() => setSelected(null)} aria-label="Close details">×</button>
          {selected.node && (
            <NodeDetail node={selected.node} net={net}
              onExpand={() => setSeeds([...seeds, selected.node!.name])}
              onAsk={() => navigate(`/${vault}/chat`, { state: { prefill: `What phosphorylates ${selected.node!.name}, and what does it phosphorylate?` } })} />
          )}
          {selected.edge && <EdgeDetail edge={selected.edge} enzyme={byId.get(selected.edge.source)} target={byId.get(selected.edge.target)} />}
        </aside>
      )}
    </div>
  );
}

function SeedPicker({ seeds, onChange }: { seeds: string[]; onChange: (s: string[]) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ id: string; name: string; type: string; kinase_group: string | null }[]>([]);
  useEffect(() => {
    if (q.trim().length < 2) { setHits([]); return; }
    const t = setTimeout(() => graphApi.search(q.trim()).then(setHits).catch(() => setHits([])), 200);
    return () => clearTimeout(t);
  }, [q]);
  const add = (name: string) => { onChange([...seeds, name]); setQ(""); setHits([]); };
  return (
    <div className="seed-picker">
      <label className="field-label" htmlFor="seed-search">Proteins</label>
      <div className="chips">
        {seeds.map((s) => (
          <span key={s} className="chip chip-new">{s}
            <button className="link-btn" aria-label={`Remove ${s}`} onClick={() => onChange(seeds.filter((x) => x !== s))}>×</button>
          </span>
        ))}
      </div>
      <input id="seed-search" placeholder="Add a gene or UniProt id..." value={q} autoComplete="off"
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (hits[0] || q.trim())) { e.preventDefault(); add(hits[0]?.name ?? q.trim()); } }} />
      {hits.length > 0 && (
        <ul className="suggest" role="listbox">
          {hits.slice(0, 8).map((h) => (
            <li key={h.id}><button type="button" onClick={() => add(h.name)}>
              <strong>{h.name}</strong> <span className="muted small">{h.id} · {h.kinase_group ?? h.type}</span>
            </button></li>
          ))}
        </ul>
      )}
    </div>
  );
}

function NodeDetail({ node, net, onExpand, onAsk }: { node: NetNode; net: Network | null; onExpand: () => void; onAsk: () => void }) {
  const regs = net?.edges.filter((e) => e.target === node.id).length ?? 0;
  const tgts = net?.edges.filter((e) => e.source === node.id).length ?? 0;
  const uniprot = /^[A-Z0-9]{6,10}(-\d+)?$/.test(node.id);
  return (
    <div className="detail">
      <h2><i className="swatch" style={{ background: nodeColor(node) }} />{node.name}</h2>
      <dl>
        <dt>ID</dt><dd>{uniprot ? <a href={`https://www.uniprot.org/uniprotkb/${node.id.split("-")[0]}`} target="_blank" rel="noreferrer">{node.id}</a> : node.id}</dd>
        <dt>Type</dt><dd>{node.type}{node.role !== "other" ? ` · ${node.role}` : ""}</dd>
        {node.kinase_group && <><dt>Kinase group</dt><dd>{node.kinase_group}{node.kinase_family ? ` · ${node.kinase_family}` : ""}</dd></>}
        <dt>In this view</dt><dd>{regs} regulator{regs === 1 ? "" : "s"}, {tgts} target{tgts === 1 ? "" : "s"}</dd>
      </dl>
      <div className="detail-actions">
        {!node.is_seed && <button className="btn btn-small" onClick={onExpand}>Expand</button>}
        <button className="btn btn-small btn-ghost" onClick={onAsk}>Ask in chat</button>
      </div>
    </div>
  );
}

function EdgeDetail({ edge, enzyme, target }: { edge: NetEdge; enzyme?: NetNode; target?: NetNode }) {
  const [info, setInfo] = useState<Interaction | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setInfo(null);
    graphApi.interaction(edge.source, edge.target).then(setInfo).catch((e) => setError(e.message));
  }, [edge.source, edge.target]);
  const records = info?.interactions.find((i) => i.mechanism === edge.mechanism)?.records ?? [];
  return (
    <div className="detail">
      <h2>{enzyme?.name ?? edge.source} → {target?.name ?? edge.target}</h2>
      <dl>
        <dt>Mechanism</dt><dd>{edge.mechanism}</dd>
        <dt>Sites</dt><dd>{edge.sites.join(", ") || "-"}</dd>
        <dt>Effect</dt><dd>{edge.effect ?? "not reported"}</dd>
        <dt>SIGNOR score</dt><dd>{edge.score ?? "- (not in SIGNOR)"}</dd>
        <dt>Databases</dt><dd>{edge.sources.join(", ")}</dd>
      </dl>
      {error && <div className="error">{error}</div>}
      {!info && !error && <div className="muted small pulse">Loading records...</div>}
      {records.length > 0 && (
        <table className="records">
          <thead><tr><th>Database</th><th>Evidence</th><th>PubMed</th></tr></thead>
          <tbody>
            {records.map((r, i) => (
              <tr key={i}>
                <td>{r.source}{r.signor_id && <><br /><a className="small" href={`https://signor.uniroma2.it/relation_result.php?id=${r.signor_id}`} target="_blank" rel="noreferrer">{r.signor_id}</a></>}</td>
                <td className="small">{r.evidence || (r.score != null ? `score ${r.score}` : "-")}</td>
                <td className="small">
                  {(r.pmids ?? []).slice(0, 6).map((p) => (
                    <a key={p} href={`https://pubmed.ncbi.nlm.nih.gov/${p}/`} target="_blank" rel="noreferrer">{p} </a>
                  ))}
                  {(r.pmids?.length ?? 0) > 6 && <span className="muted">+{r.pmids!.length - 6}</span>}
                  {!r.pmids?.length && <span className="muted">none listed</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
