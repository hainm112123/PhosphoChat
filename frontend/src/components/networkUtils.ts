import type { NetEdge, NetNode, Network } from "../api";

/** Merge several networks (e.g. from several tool calls in one chat answer). */
export function mergeNetworks(nets: Network[]): Network {
  const nodes = new Map<string, NetNode>();
  const edges = new Map<string, NetEdge>();
  for (const n of nets) {
    n.nodes.forEach((x) => nodes.set(x.id, nodes.has(x.id) ? { ...x, is_seed: x.is_seed || nodes.get(x.id)!.is_seed } : x));
    n.edges.forEach((e) => edges.set(e.id, e));
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}
