import { useState } from "react";
import { useParams } from "react-router-dom";

import { useFindingDispositions } from "@/features/cases/hooks/useFindingDispositions";
import { EXPORT_HISTORY_BY_CASE, REPORT_SECTIONS_BY_CASE } from "@/shared/mock/reports";

const EXPORT_LATENCY_MS = 1100;

/** Reports tab (frontend-architecture.md §25) — `GET/POST /cases/{id}/reports`, async export job. */
export function CaseReportsPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const resolvedCaseId = caseId ?? "";
  const findings = useFindingDispositions(resolvedCaseId);
  const sections = REPORT_SECTIONS_BY_CASE[resolvedCaseId] ?? [];
  const exports = EXPORT_HISTORY_BY_CASE[resolvedCaseId] ?? [];

  const [sectionsOff, setSectionsOff] = useState<Record<string, boolean>>({});
  const [exportBusy, setExportBusy] = useState(false);

  const openFindings = findings.filter((f) => !f.disposition).length;

  if (sections.length === 0) {
    return (
      <main style={{ width: "100%", maxWidth: 1440, margin: "0 auto", padding: 22.4 }}>
        <p className="sc-text-muted" style={{ fontSize: 13 }}>
          No report has been drafted for case {resolvedCaseId} yet.
        </p>
      </main>
    );
  }

  return (
    <main
      data-screen-label="Reports"
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
      <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 16.8 }}>
        <div>
          <h4 style={{ margin: "0 0 2.8px" }}>Incident report · {resolvedCaseId}</h4>
          <p className="sc-text-muted" style={{ fontSize: 13, margin: 0 }}>
            Draft v3 · every included section cites the evidence it rests on. Rejected findings are
            excluded automatically.
          </p>
        </div>

        <section>
          <h5 style={{ margin: "0 0 8.4px" }}>Sections</h5>
          <div style={{ display: "flex", flexDirection: "column", gap: 5.6 }}>
            {sections.map((sec) => {
              const meta =
                sec.title === "Findings and dispositions"
                  ? `${String(openFindings)} open`
                  : sec.meta;
              return (
                <div
                  key={sec.title}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 11.2,
                    padding: "8.4px 11.2px",
                    borderRadius: 8,
                    background: "var(--sc-surface)",
                  }}
                >
                  <label className="radio" style={{ gap: 8.4 }}>
                    <input
                      type="checkbox"
                      checked={!sectionsOff[sec.title]}
                      onChange={() => {
                        setSectionsOff((prev) => ({ ...prev, [sec.title]: !prev[sec.title] }));
                      }}
                    />
                    <span className="sc-dot-toggle" />
                    <span style={{ fontSize: 13 }}>{sec.title}</span>
                  </label>
                  <span className="sc-text-muted" style={{ fontSize: 11, marginLeft: "auto" }}>
                    {meta}
                  </span>
                  <span
                    className={sec.state === "Drafted" ? "tag tag-accent-2" : "tag tag-neutral"}
                  >
                    {sec.state}
                  </span>
                </div>
              );
            })}
          </div>
        </section>

        <section>
          <h5 style={{ margin: "0 0 8.4px" }}>Findings included</h5>
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 88 }}>Finding</th>
                <th>Statement</th>
                <th style={{ width: 108 }}>Disposition</th>
                <th style={{ width: 96 }}>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {findings.map((f) => {
                const disp = f.disposition ?? "Open lead";
                return (
                  <tr key={f.id}>
                    <td className="sc-code" style={{ color: "var(--sc-accent-300)" }}>
                      {f.id}
                    </td>
                    <td style={{ fontSize: 13 }}>{f.title}</td>
                    <td>
                      <span className={disp === "Open lead" ? "tag tag-neutral" : "tag tag-accent"}>
                        {disp}
                      </span>
                    </td>
                    <td className="sc-text-muted sc-code" style={{ fontSize: 11 }}>
                      {f.sources.length} items
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="sc-text-muted" style={{ fontSize: 11, margin: "8.4px 0 0" }}>
            Unreviewed findings appear as open leads and are marked as such in the export.
          </p>
        </section>
      </div>

      <aside
        style={{ display: "flex", flexDirection: "column", gap: 11.2, position: "sticky", top: 78 }}
      >
        <div className="card elev-sm">
          <span className="card-kicker">Export</span>
          <div className="field" style={{ marginTop: 2.8 }}>
            <label htmlFor="sai-audience">Audience</label>
            <div className="seg" style={{ width: "100%" }} id="sai-audience">
              <label className="seg-opt">
                <input type="radio" name="sai-aud" defaultChecked />
                <span>Internal</span>
              </label>
              <label className="seg-opt">
                <input type="radio" name="sai-aud" />
                <span>Counsel</span>
              </label>
              <label className="seg-opt">
                <input type="radio" name="sai-aud" />
                <span>Regulator</span>
              </label>
            </div>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 5.6, marginTop: 8.4 }}>
            <button
              type="button"
              className="btn btn-primary btn-block"
              disabled={exportBusy}
              onClick={() => {
                setExportBusy(true);
                setTimeout(() => {
                  setExportBusy(false);
                }, EXPORT_LATENCY_MS);
              }}
            >
              <i className="ph ph-file-pdf" style={{ fontSize: 15 }} />{" "}
              {exportBusy ? "Generating…" : "Export as PDF"}
            </button>
            <button type="button" className="btn btn-secondary btn-block">
              <i className="ph ph-brackets-curly" style={{ fontSize: 15 }} /> Export as JSON bundle
            </button>
          </div>
          <p className="sc-text-muted" style={{ fontSize: 11, margin: "8.4px 0 0" }}>
            Exports are hashed and written to the case audit trail with the requesting analyst.
          </p>
        </div>
        <div className="card elev-sm">
          <span className="card-kicker">Export history</span>
          {exports.map((x) => (
            <div
              key={`${x.what}-${x.when}`}
              style={{ display: "flex", flexDirection: "column", gap: 1, padding: "2.8px 0" }}
            >
              <span style={{ fontSize: 12 }}>{x.what}</span>
              <span className="sc-text-muted" style={{ fontSize: 11 }}>
                {x.who} · {x.when}
              </span>
            </div>
          ))}
        </div>
      </aside>
    </main>
  );
}
