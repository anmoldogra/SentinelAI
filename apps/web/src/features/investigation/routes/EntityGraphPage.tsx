import { useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { computeGraphLayout, selectedEntity } from "@/features/investigation/graphLayout";
import {
  DEFAULT_PIVOT_BY_CASE,
  ENTITY_KINDS_BY_CASE,
  GRAPH_RELATIONS_BY_CASE,
} from "@/shared/mock/graph";

const LEGEND: { label: string; color: string }[] = [
  { label: "Pivot entity", color: "#9184d9" },
  { label: "Depth 1", color: "#b5abfc" },
  { label: "Depth 2", color: "#75798c" },
];

/** Entity graph (frontend-architecture.md §27) — `GET /cases/{id}/graph`, depth-bounded traversal. */
export function EntityGraphPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const resolvedCaseId = caseId ?? "";
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const kinds = useMemo(() => ENTITY_KINDS_BY_CASE[resolvedCaseId] ?? {}, [resolvedCaseId]);
  const relations = useMemo(() => GRAPH_RELATIONS_BY_CASE[resolvedCaseId] ?? [], [resolvedCaseId]);
  const defaultPivot = DEFAULT_PIVOT_BY_CASE[resolvedCaseId];

  const initialPivot = searchParams.get("pivot") ?? defaultPivot;

  const [depth, setDepth] = useState<1 | 2>(1);
  const [pivot, setPivot] = useState<string | undefined>(initialPivot);
  const [picked, setPicked] = useState<string | undefined>(initialPivot);

  const { nodes, edges } = useMemo(
    () =>
      pivot
        ? computeGraphLayout(kinds, relations, pivot, depth, picked ?? pivot)
        : { nodes: [], edges: [] },
    [kinds, relations, pivot, depth, picked],
  );

  const selected = picked ? selectedEntity(kinds, relations, resolvedCaseId, picked) : undefined;

  if (!pivot) {
    return (
      <main style={{ width: "100%", maxWidth: 1440, margin: "0 auto", padding: 22.4 }}>
        <p className="sc-text-muted" style={{ fontSize: 13 }}>
          No entity graph has been seeded for case {resolvedCaseId} yet.
        </p>
      </main>
    );
  }

  return (
    <main
      data-screen-label="Entity graph"
      style={{
        width: "100%",
        maxWidth: 1440,
        margin: "0 auto",
        padding: 22.4,
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) 320px",
        gap: 22.4,
        alignItems: "start",
      }}
    >
      <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 11.2 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 11.2, flexWrap: "wrap" }}>
          <h4 style={{ margin: 0 }}>Entity graph</h4>
          <span className="sc-code" style={{ color: "var(--sc-accent-300)" }}>
            {resolvedCaseId}
          </span>
          <span className="sc-text-muted" style={{ fontSize: 12 }}>
            {Object.keys(kinds).length} entities · showing depth {depth} from {pivot}
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8.4, flexWrap: "wrap" }}>
          <div className="seg">
            <label className="seg-opt">
              <input
                type="radio"
                name="sai-depth"
                checked={depth === 1}
                onChange={() => {
                  setDepth(1);
                }}
              />
              <span>Depth 1</span>
            </label>
            <label className="seg-opt">
              <input
                type="radio"
                name="sai-depth"
                checked={depth === 2}
                onChange={() => {
                  setDepth(2);
                }}
              />
              <span>Depth 2</span>
            </label>
          </div>
          <span className="sc-text-muted" style={{ fontSize: 12, marginLeft: "auto" }}>
            Traversal capped at 250 nodes to keep queries bounded.
          </span>
        </div>
        <div
          style={{
            borderRadius: 14,
            background: "var(--sc-surface)",
            boxShadow: "var(--sc-shadow-sm)",
            padding: 11.2,
          }}
        >
          <svg
            viewBox="0 0 720 460"
            style={{ width: "100%", height: "auto", display: "block" }}
            role="img"
            aria-label={`Entity relationship graph for ${resolvedCaseId}`}
          >
            {edges.map((e) => (
              <g key={e.key}>
                <line
                  x1={e.x1}
                  y1={e.y1}
                  x2={e.x2}
                  y2={e.y2}
                  stroke={e.stroke}
                  strokeWidth={e.width}
                />
                <text
                  x={e.mx}
                  y={e.my}
                  fill="#75798c"
                  fontSize={9}
                  textAnchor="middle"
                  fontFamily="ui-monospace, Menlo, monospace"
                >
                  {e.label}
                </text>
              </g>
            ))}
            {nodes.map((n) => (
              <g
                key={n.id}
                onClick={() => {
                  setPicked(n.id);
                }}
                style={{ cursor: "pointer" }}
              >
                <circle
                  cx={n.x}
                  cy={n.y}
                  r={n.r}
                  fill={n.fill}
                  stroke={n.stroke}
                  strokeWidth={1.5}
                />
                <text
                  x={n.x}
                  y={n.labelY}
                  fill={n.textFill}
                  fontSize={11}
                  textAnchor="middle"
                  fontFamily="Inter, sans-serif"
                >
                  {n.id}
                </text>
                <text
                  x={n.x}
                  y={n.kindY}
                  fill="#75798c"
                  fontSize={9}
                  textAnchor="middle"
                  fontFamily="Inter, sans-serif"
                >
                  {n.kind}
                </text>
              </g>
            ))}
          </svg>
        </div>
        <div style={{ display: "flex", gap: 11.2, flexWrap: "wrap" }}>
          {LEGEND.map((l) => (
            <span
              key={l.label}
              className="sc-text-muted"
              style={{ display: "flex", alignItems: "center", gap: 5.6, fontSize: 11 }}
            >
              <i className="ph-fill ph-circle" style={{ fontSize: 9, color: l.color }} /> {l.label}
            </span>
          ))}
        </div>
      </div>

      <aside
        style={{ display: "flex", flexDirection: "column", gap: 11.2, position: "sticky", top: 78 }}
      >
        {selected && (
          <>
            <div className="card elev-sm">
              <span className="card-kicker">{selected.kind}</span>
              <span className="card-title" style={{ fontSize: 16 }}>
                {selected.id}
              </span>
              <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, opacity: 0.85 }}>
                {selected.note}
              </p>
              <div style={{ display: "flex", flexDirection: "column", gap: 5.6, marginTop: 5.6 }}>
                {selected.facts.map((f) => (
                  <div key={f.k} style={{ display: "flex", gap: 8.4, fontSize: 12 }}>
                    <span className="sc-text-muted">{f.k}</span>
                    <span style={{ marginLeft: "auto", textAlign: "right" }}>{f.v}</span>
                  </div>
                ))}
              </div>
              <div style={{ display: "flex", gap: 5.6, marginTop: 8.4 }}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    setPivot(selected.id);
                  }}
                >
                  Pivot here
                </button>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => {
                    void navigate(`/cases/${resolvedCaseId}/evidence`);
                  }}
                >
                  Linked evidence
                </button>
              </div>
            </div>
            <div className="card elev-sm">
              <span className="card-kicker">Relationships</span>
              {selected.relationships.map((r) => (
                <div
                  key={`${r.type}-${r.target}`}
                  style={{ display: "flex", gap: 8.4, fontSize: 12, padding: "2.8px 0" }}
                >
                  <span className="sc-code" style={{ color: "var(--sc-accent-300)" }}>
                    {r.type}
                  </span>
                  <span style={{ marginLeft: "auto", textAlign: "right" }}>{r.target}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </aside>
    </main>
  );
}
