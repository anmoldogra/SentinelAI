import { useOutletContext } from "react-router-dom";

import type { CaseOutletContext } from "@/features/cases/routes/CaseLayout";
import { EVIDENCE_BY_CASE } from "@/shared/mock/evidence";

/** Evidence tab (frontend-architecture.md §25, §26) — `GET /cases/{id}/evidence`. */
export function CaseEvidencePage() {
  const { caseId } = useOutletContext<CaseOutletContext>();
  const evidence = EVIDENCE_BY_CASE[caseId] ?? [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 11.2 }}>
      <div style={{ display: "flex", gap: 5.6, alignItems: "center", flexWrap: "wrap" }}>
        <input
          className="input"
          placeholder="Filter by hash, path, entity…"
          style={{ maxWidth: 280 }}
        />
        <button type="button" className="btn btn-secondary">
          <i className="ph ph-funnel" style={{ fontSize: 15 }} /> Source
        </button>
        <button type="button" className="btn btn-secondary">
          <i className="ph ph-clock" style={{ fontSize: 15 }} /> Collected
        </button>
        <button type="button" className="btn btn-primary" style={{ marginLeft: "auto" }}>
          <i className="ph ph-upload-simple" style={{ fontSize: 15 }} /> Ingest evidence
        </button>
      </div>
      {evidence.length === 0 ? (
        <p className="sc-text-muted" style={{ fontSize: 13 }}>
          No mock evidence has been seeded for this case yet.
        </p>
      ) : (
        <>
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 116 }}>Evidence</th>
                <th>Artifact</th>
                <th style={{ width: 132 }}>Source</th>
                <th style={{ width: 128 }}>Collected</th>
                <th style={{ width: 116 }}>Integrity</th>
                <th style={{ width: 96 }}>Custody</th>
              </tr>
            </thead>
            <tbody>
              {evidence.map((e) => (
                <tr key={e.id}>
                  <td className="sc-code" style={{ color: "var(--sc-accent-300)" }}>
                    {e.id}
                  </td>
                  <td>
                    <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                      <span>{e.name}</span>
                      <span className="sc-text-muted sc-code" style={{ fontSize: 11 }}>
                        {e.hash}
                      </span>
                    </div>
                  </td>
                  <td className="sc-text-muted" style={{ fontSize: 13 }}>
                    {e.source}
                  </td>
                  <td className="sc-text-muted" style={{ fontSize: 13 }}>
                    {e.collected}
                  </td>
                  <td>
                    <span
                      className={
                        e.integrity === "Verified" ? "tag tag-accent-2" : "tag tag-neutral"
                      }
                      style={{ gap: 4 }}
                    >
                      <i
                        className={`ph ${e.integrity === "Verified" ? "ph-seal-check" : "ph-hourglass"}`}
                        style={{ fontSize: 12 }}
                      />{" "}
                      {e.integrity}
                    </span>
                  </td>
                  <td className="sc-text-muted" style={{ fontSize: 13 }}>
                    {e.custody}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="sc-text-muted" style={{ fontSize: 11, margin: 0 }}>
            Hashes verified on ingest and on every read; a mismatch escalates to the
            tamper-detection alert path.
          </p>
        </>
      )}
    </div>
  );
}
