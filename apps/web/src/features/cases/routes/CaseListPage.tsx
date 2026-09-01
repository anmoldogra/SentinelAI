import { useNavigate } from "react-router-dom";

import { severityTagClass } from "@/shared/console/severity";
import { CASE_SUMMARIES } from "@/shared/mock/cases";

/** Case list (frontend-architecture.md §25) — `GET /cases`, scoped to the caller's visible cases. */
export function CaseListPage() {
  const navigate = useNavigate();

  return (
    <main
      data-screen-label="Cases"
      style={{ width: "100%", maxWidth: 1440, margin: "0 auto", padding: 22.4 }}
    >
      <div style={{ marginBottom: 16.8 }}>
        <h4 style={{ margin: "0 0 2.8px" }}>Cases</h4>
        <p className="sc-text-muted" style={{ fontSize: 13, margin: 0 }}>
          {CASE_SUMMARIES.length} cases visible to you.
        </p>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th style={{ width: 108 }}>Case</th>
            <th>Title</th>
            <th style={{ width: 92 }}>Severity</th>
            <th style={{ width: 128 }}>Stage</th>
            <th style={{ width: 90 }}>Findings</th>
            <th style={{ width: 96 }}>Updated</th>
          </tr>
        </thead>
        <tbody>
          {CASE_SUMMARIES.map((c) => (
            <tr
              key={c.id}
              className="sc-row"
              onClick={() => {
                void navigate(`/cases/${c.id}`);
              }}
            >
              <td className="sc-code" style={{ color: "var(--sc-accent-300)" }}>
                {c.id}
              </td>
              <td>{c.title}</td>
              <td>
                <span className={severityTagClass(c.severity)}>{c.severity}</span>
              </td>
              <td className="sc-text-muted" style={{ fontSize: 13 }}>
                {c.stage}
              </td>
              <td>{c.findingsOpen}</td>
              <td className="sc-text-muted" style={{ fontSize: 13 }}>
                {c.updated}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
