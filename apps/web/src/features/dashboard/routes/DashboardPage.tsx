import { useNavigate } from "react-router-dom";

import { dotColor, severityTagClass } from "@/shared/console/severity";
import { CASE_SUMMARIES } from "@/shared/mock/cases";
import {
  CONNECTORS,
  DASHBOARD_QUEUE,
  DASHBOARD_STATS,
  RECENT_AUDIT,
} from "@/shared/mock/dashboard";

const TODAY = new Date().toLocaleDateString("en-US", {
  day: "2-digit",
  month: "short",
  year: "numeric",
});

/**
 * Post-login landing screen (frontend-architecture.md §23) — task-oriented widgets, each a
 * would-be independent query (`GET /cases?status=open`, the review queue, `GET /notifications`,
 * §23's table) standing in as shared/mock data until those endpoints exist.
 */
export function DashboardPage() {
  const navigate = useNavigate();

  return (
    <main
      data-screen-label="Dashboard"
      style={{
        width: "100%",
        maxWidth: 1440,
        margin: "0 auto",
        padding: 22.4,
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) 340px",
        gap: 22.4,
        alignItems: "start",
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 22.4, minWidth: 0 }}>
        <div>
          <h4 style={{ margin: "0 0 2.8px" }}>Shift handover · {TODAY}</h4>
          <p className="sc-text-muted" style={{ fontSize: 13, margin: 0 }}>
            6 cases assigned to you · 14 AI findings await disposition · 2 connectors degraded.
          </p>
        </div>

        <div
          style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 11.2 }}
        >
          {DASHBOARD_STATS.map((stat) => (
            <div key={stat.label} className="card elev-sm" style={{ gap: 2.8 }}>
              <span className="card-kicker">{stat.label}</span>
              <span style={{ fontFamily: "var(--sc-font-heading)", fontSize: 25, lineHeight: 1.1 }}>
                {stat.value}
              </span>
              <span className="sc-text-muted" style={{ fontSize: 11 }}>
                {stat.note}
              </span>
            </div>
          ))}
        </div>

        <section>
          <div style={{ display: "flex", alignItems: "baseline", gap: 11.2, marginBottom: 8.4 }}>
            <h5 style={{ margin: 0 }}>Assigned cases</h5>
            <button
              type="button"
              className="btn btn-ghost"
              style={{ marginLeft: "auto", fontSize: 12, padding: 0 }}
              onClick={() => {
                void navigate("/cases");
              }}
            >
              All cases
            </button>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 108 }}>Case</th>
                <th>Title</th>
                <th style={{ width: 92 }}>Severity</th>
                <th style={{ width: 108 }}>Stage</th>
                <th style={{ width: 76 }}>Findings</th>
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
        </section>

        <section>
          <h5 style={{ margin: "0 0 8.4px" }}>Findings awaiting your disposition</h5>
          <div style={{ display: "flex", flexDirection: "column", gap: 8.4 }}>
            {DASHBOARD_QUEUE.map((q) => (
              <div key={`${q.caseId}-${q.title}`} className="card elev-sm" style={{ gap: 5.6 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8.4 }}>
                  <span className="sc-code" style={{ color: "var(--sc-accent-300)" }}>
                    {q.caseId}
                  </span>
                  <span className="tag tag-outline">{q.confidence}</span>
                  <span className="sc-text-muted" style={{ fontSize: 11, marginLeft: "auto" }}>
                    {q.age}
                  </span>
                </div>
                <span className="card-title" style={{ fontSize: 15 }}>
                  {q.title}
                </span>
                <span className="sc-text-muted" style={{ fontSize: 12 }}>
                  {q.basis}
                </span>
              </div>
            ))}
          </div>
        </section>
      </div>

      <aside
        style={{ display: "flex", flexDirection: "column", gap: 16.8, position: "sticky", top: 78 }}
      >
        <div className="card elev-sm">
          <span className="card-kicker">Ingest health</span>
          <div style={{ display: "flex", flexDirection: "column", gap: 8.4, marginTop: 2.8 }}>
            {CONNECTORS.map((k) => (
              <div key={k.name} style={{ display: "flex", alignItems: "center", gap: 8.4 }}>
                <i className="ph-fill ph-circle" style={{ fontSize: 8, color: dotColor(k.dot) }} />
                <span style={{ fontSize: 13 }}>{k.name}</span>
                <span className="sc-text-muted" style={{ fontSize: 11, marginLeft: "auto" }}>
                  {k.detail}
                </span>
              </div>
            ))}
          </div>
        </div>
        <div className="card elev-sm">
          <span className="card-kicker">Recent audit trail</span>
          <div style={{ display: "flex", flexDirection: "column", gap: 8.4, marginTop: 2.8 }}>
            {RECENT_AUDIT.map((a) => (
              <div
                key={`${a.what}-${a.when}`}
                style={{ display: "flex", flexDirection: "column", gap: 1 }}
              >
                <span style={{ fontSize: 12 }}>{a.what}</span>
                <span className="sc-text-muted" style={{ fontSize: 11 }}>
                  {a.who} · {a.when}
                </span>
              </div>
            ))}
          </div>
        </div>
      </aside>
    </main>
  );
}
