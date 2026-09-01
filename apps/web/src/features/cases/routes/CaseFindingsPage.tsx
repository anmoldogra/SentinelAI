import { useOutletContext } from "react-router-dom";

import type { CaseOutletContext } from "@/features/cases/routes/CaseLayout";

/**
 * AI findings tab (frontend-architecture.md §24) — the per-case slice of the finding review
 * workflow. Disposition actions are non-optimistic (§10, PRD FR-7.3): see
 * features/cases/hooks/useFindingDispositions.ts for the pending-state mechanics.
 */
export function CaseFindingsPage() {
  const { findings, askCopilot } = useOutletContext<CaseOutletContext>();

  if (findings.length === 0) {
    return (
      <p className="sc-text-muted" style={{ fontSize: 13 }}>
        No AI findings have been generated for this case yet.
      </p>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 11.2 }}>
      <p className="sc-text-muted" style={{ fontSize: 12, margin: 0 }}>
        Every finding shows the evidence it was derived from. Dispositions are recorded only after
        the server confirms — nothing changes optimistically.
      </p>
      {findings.map((f) => (
        <div key={f.id} className="card elev-sm" style={{ gap: 8.4 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8.4, flexWrap: "wrap" }}>
            <span className="sc-code" style={{ color: "var(--sc-accent-300)" }}>
              {f.id}
            </span>
            <span className="tag tag-outline">{f.confidence} confidence</span>
            <span className="tag tag-neutral">{f.kind}</span>
            <span className={f.stateClass} style={{ marginLeft: "auto" }}>
              {f.stateLabel}
            </span>
          </div>
          <span className="card-title" style={{ fontSize: 16 }}>
            {f.title}
          </span>
          <p style={{ margin: 0, fontSize: 13, lineHeight: 1.5, opacity: 0.85 }}>{f.reasoning}</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5.6, alignItems: "center" }}>
            <span
              className="sc-text-muted"
              style={{ fontSize: 11, letterSpacing: "0.08em", textTransform: "uppercase" }}
            >
              Derived from
            </span>
            {f.sources.map((src) => (
              <span key={src} className="tag tag-accent-2 sc-code">
                {src}
              </span>
            ))}
          </div>
          <div style={{ display: "flex", gap: 5.6, marginTop: 2.8 }}>
            <button type="button" className="btn btn-primary" disabled={f.busy} onClick={f.confirm}>
              <i className="ph ph-check" style={{ fontSize: 15 }} /> Confirm
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={f.busy}
              onClick={f.reject}
            >
              <i className="ph ph-x" style={{ fontSize: 15 }} /> Reject
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              disabled={f.busy}
              onClick={f.escalate}
            >
              <i className="ph ph-arrow-up-right" style={{ fontSize: 15 }} /> Escalate
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              style={{ marginLeft: "auto" }}
              onClick={() => {
                askCopilot(`Explain the basis for ${f.id} and list any contradicting evidence.`);
              }}
            >
              Ask copilot about this
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
