import { useOutletContext } from "react-router-dom";

import type { CaseOutletContext } from "@/features/cases/routes/CaseLayout";

/** Overview tab (frontend-architecture.md §25) — `GET /cases/{id}`. */
export function CaseOverviewPage() {
  const { detail } = useOutletContext<CaseOutletContext>();

  if (!detail) {
    return (
      <div className="card elev-sm">
        <span className="card-kicker">No overview yet</span>
        <p style={{ margin: 0, fontSize: 13, opacity: 0.85 }}>
          No mock detail has been seeded for this case yet.
        </p>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16.8 }}>
      <div className="card elev-sm">
        <span className="card-kicker">Working hypothesis · analyst-authored</span>
        <p style={{ margin: 0, fontSize: 14, lineHeight: 1.5 }}>{detail.hypothesis}</p>
        <div className="card-meta" style={{ marginTop: 5.6 }}>
          <span>
            Last edited by {detail.hypothesisEditedBy} · {detail.hypothesisEditedAgo}
          </span>
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 11.2 }}>
        {detail.overviewStats.map((o) => (
          <div key={o.label} className="card elev-sm" style={{ gap: 2.8 }}>
            <span className="card-kicker">{o.label}</span>
            <span style={{ fontFamily: "var(--sc-font-heading)", fontSize: 22, lineHeight: 1.1 }}>
              {o.value}
            </span>
            <span className="sc-text-muted" style={{ fontSize: 11 }}>
              {o.note}
            </span>
          </div>
        ))}
      </div>
      <div>
        <h5 style={{ margin: "0 0 8.4px" }}>Key entities</h5>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 5.6 }}>
          {detail.keyEntities.map((e) => (
            <span key={e.label} className="tag tag-outline" style={{ gap: 5.6 }}>
              <i className={`ph ${e.icon}`} style={{ fontSize: 13 }} /> {e.label}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
