import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
// @ts-expect-error - cytoscape-fcose ships no type definitions
import fcose from "cytoscape-fcose";
import type { NetEdge, NetNode, Network } from "../api";

cytoscape.use(fcose);

/** Kinase group colours (KiNet-style), plus roles for proteins without a group. */
export const GROUP_COLORS: Record<string, string> = {
  TK: "#1f77b4", Other: "#aec7e8", CAMK: "#ff7f0e", CMGC: "#ffbb78", AGC: "#2ca02c",
  STE: "#98df8a", TKL: "#d62728", Atypical: "#ff9896", CK1: "#9467bd", RGC: "#c5b0d5",
};
export const ROLE_COLORS = { kinase: "#bcbd22", phosphatase: "#e377c2", other: "#8a8f98" };
export const EFFECT_COLORS = { up: "#2f9e44", down: "#e03131", unknown: "#9aa0a6" };
/** Claims from the vault's own sources: purple, dotted, so they never look like database links. */
export const CLAIM_COLOR = "#8e44ad";

export function nodeColor(n: NetNode): string {
  return (n.kinase_group && GROUP_COLORS[n.kinase_group]) || ROLE_COLORS[n.role] || ROLE_COLORS.other;
}

export function effectKind(effect: string | null): keyof typeof EFFECT_COLORS {
  if (!effect) return "unknown";
  if (effect.startsWith("up")) return "up";
  if (effect.startsWith("down")) return "down";
  return "unknown";
}

export type NetworkHandle = { png: () => string | null; fit: () => void };

type Props = {
  data: Network;
  height?: number | string;
  compact?: boolean;
  onNode?: (n: NetNode) => void;
  onEdge?: (e: NetEdge) => void;
};

function cssVar(name: string, fallback: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

/** Interactive kinase-substrate network (Cytoscape.js + fcose layout). */
export const NetworkView = forwardRef<NetworkHandle, Props>(function NetworkView(
  { data, height = 520, compact = false, onNode, onEdge }, ref,
) {
  const box = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const handlers = useRef({ onNode, onEdge });
  handlers.current = { onNode, onEdge };

  useImperativeHandle(ref, () => ({
    png: () => cy.current?.png({ full: true, scale: 2, bg: cssVar("--panel", "#ffffff") }) ?? null,
    fit: () => cy.current?.fit(undefined, 30),
  }));

  useEffect(() => {
    if (!box.current) return;
    const text = cssVar("--text", "#1d1d1b");
    const panel = cssVar("--panel", "#ffffff");
    const nodes: ElementDefinition[] = data.nodes.map((n) => ({
      data: { ...n, id: n.id, label: n.name, color: nodeColor(n) },
      classes: n.is_seed ? "seed" : "",
    }));
    const known = new Set(data.nodes.map((n) => n.id));
    const edges: ElementDefinition[] = data.edges
      .filter((e) => known.has(e.source) && known.has(e.target))
      .map((e) => ({
        data: { ...e, id: e.id, color: e.claim ? CLAIM_COLOR : EFFECT_COLORS[effectKind(e.effect)],
                width: e.claim ? 2.5 : 1 + e.n_sources },
        classes: [e.mechanism === "dephosphorylation" ? "dephos" : "", e.source === e.target ? "loop" : "",
                  e.claim ? `claim claim-${e.claim_status}` : ""].join(" "),
      }));
    const instance = cytoscape({
      container: box.current,
      elements: [...nodes, ...edges],
      wheelSensitivity: 0.25,
      minZoom: 0.2,
      maxZoom: 3,
      style: [
        { selector: "node", style: {
          "background-color": "data(color)", label: "data(label)", color: text,
          "font-size": compact ? 9 : 11, "text-valign": "bottom", "text-margin-y": 3,
          width: compact ? 14 : 18, height: compact ? 14 : 18,
          "border-width": 1, "border-color": panel, "text-outline-color": panel, "text-outline-width": 2,
        } },
        { selector: "node.seed", style: { "border-width": 3, "border-color": text, width: compact ? 18 : 24, height: compact ? 18 : 24, "font-weight": "bold" } },
        { selector: "node:selected", style: { "border-width": 3, "border-color": "#f59f00" } },
        { selector: "edge", style: {
          width: "data(width)", "line-color": "data(color)", "target-arrow-color": "data(color)",
          "target-arrow-shape": "triangle", "curve-style": "bezier", opacity: 0.75, "arrow-scale": 0.9,
        } },
        { selector: "edge.dephos", style: { "line-style": "dashed", "target-arrow-shape": "tee" } },
        { selector: "edge.claim", style: { "line-style": "dotted", opacity: 0.95, "curve-style": "unbundled-bezier",
                                           "control-point-distances": 25, "control-point-weights": 0.5 } },
        { selector: "edge.claim-conflicting", style: { "line-color": "#c2255c", "target-arrow-color": "#c2255c" } },
        { selector: "edge:selected", style: { opacity: 1, "line-color": "#f59f00", "target-arrow-color": "#f59f00" } },
      ],
      layout: { name: data.nodes.length > 1 ? "fcose" : "grid", animate: false, randomize: true,
                nodeRepulsion: 6500, idealEdgeLength: compact ? 60 : 90, padding: 30 } as cytoscape.LayoutOptions,
    });
    instance.on("tap", "node", (ev) => handlers.current.onNode?.(ev.target.data() as NetNode));
    instance.on("tap", "edge", (ev) => handlers.current.onEdge?.(ev.target.data() as NetEdge));
    cy.current = instance;
    return () => {
      instance.destroy();
      cy.current = null;
    };
  }, [data, compact]);

  return <div ref={box} className="network-view" style={{ height }} role="img"
    aria-label={`Network of ${data.nodes.length} proteins and ${data.edges.length} links`} />;
});

export function GroupLegend({ groups }: { groups: string[] }) {
  return (
    <div className="legend">
      {groups.map((g) => <span key={g} className="legend-item"><i style={{ background: GROUP_COLORS[g] }} />{g}</span>)}
      <span className="legend-item"><i style={{ background: ROLE_COLORS.kinase }} />Kinase (unclassified)</span>
      <span className="legend-item"><i style={{ background: ROLE_COLORS.phosphatase }} />Phosphatase</span>
      <span className="legend-item"><i style={{ background: ROLE_COLORS.other }} />Other protein</span>
      <span className="legend-item"><b style={{ background: EFFECT_COLORS.up }} />Up-regulates</span>
      <span className="legend-item"><b style={{ background: EFFECT_COLORS.down }} />Down-regulates</span>
      <span className="legend-item"><b style={{ background: EFFECT_COLORS.unknown }} />Effect unknown</span>
      <span className="legend-item"><b className="dotted" style={{ borderColor: CLAIM_COLOR }} />Claim from your sources (new / confirmed)</span>
      <span className="legend-item"><b className="dotted" style={{ borderColor: "#c2255c" }} />Claim conflicting with SIGNOR</span>
      <span className="legend-item muted small">Thicker line = more databases · dashed = dephosphorylation</span>
    </div>
  );
}
