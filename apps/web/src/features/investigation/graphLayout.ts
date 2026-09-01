import type { EntityFact, GraphRelation } from "@/shared/mock/types";

export interface GraphNodeView {
  id: string;
  x: number;
  y: number;
  depth: 0 | 1 | 2;
  r: number;
  fill: string;
  stroke: string;
  textFill: string;
  labelY: number;
  kindY: number;
  kind: string;
}

export interface GraphEdgeView {
  key: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  mx: number;
  my: number;
  label: string;
  stroke: string;
  width: number;
}

export interface SelectedEntity {
  id: string;
  kind: string;
  note: string;
  facts: { k: string; v: string }[];
  relationships: { type: string; target: string }[];
}

function neighboursOf(relations: GraphRelation[], id: string): string[] {
  return relations
    .filter((r) => r.from === id || r.to === id)
    .map((r) => (r.from === id ? r.to : r.from));
}

interface PlacedNode {
  id: string;
  x: number;
  y: number;
  depth: 0 | 1 | 2;
}

function ring(ids: string[], rx: number, ry: number, offset: number, depth: 1 | 2): PlacedNode[] {
  return ids.map((id, i) => {
    const angle = offset + (i / ids.length) * Math.PI * 2;
    return { id, x: 360 + Math.cos(angle) * rx, y: 230 + Math.sin(angle) * ry, depth };
  });
}

/**
 * Depth-1/depth-2 traversal from `pivot`, laid out on a fixed 720x460 canvas — the same layout
 * math as the approved design, kept pure/testable independent of the React component
 * (frontend-architecture.md §27's node/edge visual-encoding table).
 */
export function computeGraphLayout(
  kinds: Record<string, EntityFact>,
  relations: GraphRelation[],
  pivot: string,
  depth: 1 | 2,
  picked: string,
): { nodes: GraphNodeView[]; edges: GraphEdgeView[] } {
  const ring1Ids = [...new Set(neighboursOf(relations, pivot))];
  const ring2Ids =
    depth === 2
      ? [...new Set(ring1Ids.flatMap((id) => neighboursOf(relations, id)))].filter(
          (id) => id !== pivot && !ring1Ids.includes(id),
        )
      : [];

  const placed: PlacedNode[] = [
    { id: pivot, x: 360, y: 230, depth: 0 },
    ...ring(ring1Ids, 200, 105, -Math.PI / 2, 1),
    ...ring(ring2Ids, 315, 168, -Math.PI / 2 + 0.35, 2),
  ];

  const byId = new Map(placed.map((p) => [p.id, p]));

  const nodes: GraphNodeView[] = placed.map((p) => {
    const on = picked === p.id;
    const r = p.depth === 0 ? 13 : p.depth === 1 ? 9 : 7;
    return {
      id: p.id,
      x: p.x,
      y: p.y,
      depth: p.depth,
      r,
      fill: p.depth === 0 ? "#3a3169" : on ? "#2e2a52" : "#1f2232",
      stroke: p.depth === 0 ? "#9184d9" : p.depth === 1 ? "#b5abfc" : "#75798c",
      textFill: on || p.depth === 0 ? "#d2cefd" : "#e9e9ed",
      kind: (kinds[p.id] ?? { kind: "Entity", note: "" }).kind,
      labelY: p.y + r + 15,
      kindY: p.y + r + 27,
    };
  });

  const edges: GraphEdgeView[] = relations
    .filter((r) => byId.has(r.from) && byId.has(r.to))
    .map((r) => {
      const a = byId.get(r.from);
      const b = byId.get(r.to);
      if (!a || !b) throw new Error("unreachable: filtered above");
      const hot = picked === r.from || picked === r.to;
      return {
        key: `${r.from}->${r.to}:${r.type}`,
        x1: a.x,
        y1: a.y,
        x2: b.x,
        y2: b.y,
        mx: (a.x + b.x) / 2,
        my: (a.y + b.y) / 2 - 4,
        label: r.type,
        stroke: hot ? "#9184d9" : "#33364a",
        width: hot ? 1.6 : 1,
      };
    });

  return { nodes, edges };
}

export function selectedEntity(
  kinds: Record<string, EntityFact>,
  relations: GraphRelation[],
  caseId: string,
  picked: string,
): SelectedEntity {
  const fact = kinds[picked] ?? { kind: "Entity", note: "" };
  return {
    id: picked,
    kind: fact.kind,
    note: fact.note,
    facts: [
      { k: "First seen", v: "26 Aug 19:41" },
      { k: "Linked evidence", v: `${String(neighboursOf(relations, picked).length)} items` },
      { k: "Cases", v: caseId },
    ],
    relationships: relations
      .filter((r) => r.from === picked || r.to === picked)
      .map((r) => ({ type: r.type, target: r.from === picked ? r.to : r.from })),
  };
}
