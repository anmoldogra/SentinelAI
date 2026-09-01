import { NavLink, Outlet, useNavigate, useParams } from "react-router-dom";

import { useCopilot } from "@/features/cases/hooks/useCopilot";
import {
  useFindingDispositions,
  type FindingWithState,
} from "@/features/cases/hooks/useFindingDispositions";
import { severityTagClass } from "@/shared/console/severity";
import { CASE_DETAILS, findCaseSummary } from "@/shared/mock/cases";
import { EVIDENCE_BY_CASE } from "@/shared/mock/evidence";
import { COPILOT_SUGGESTED_PROMPTS } from "@/shared/mock/copilot";
import type { CaseDetail } from "@/shared/mock/types";

export interface CaseOutletContext {
  caseId: string;
  detail: CaseDetail | undefined;
  findings: FindingWithState[];
  askCopilot: (text: string) => void;
}

const segLinkClass = ({ isActive }: { isActive: boolean }) =>
  "seg-opt" + (isActive ? " active" : "");

/**
 * Case workspace shell (frontend-architecture.md §5's `CaseLayout`, §25): header, tab sub-nav
 * (Overview/Evidence/AI findings/Timeline as real routes, so a tab is bookmarkable per §9), and
 * the persistent investigation copilot panel (§24) that survives switching tabs since this layout
 * itself doesn't unmount between them.
 */
export function CaseLayout() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const resolvedCaseId = caseId ?? "";
  const summary = findCaseSummary(resolvedCaseId);
  const detail = CASE_DETAILS[resolvedCaseId];
  const evidenceCount = EVIDENCE_BY_CASE[resolvedCaseId]?.length ?? 0;
  const findings = useFindingDispositions(resolvedCaseId);
  const openFindings = findings.filter((f) => !f.disposition).length;
  const copilot = useCopilot(resolvedCaseId);

  if (!summary) {
    return (
      <main style={{ width: "100%", maxWidth: 1440, margin: "0 auto", padding: 22.4 }}>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => {
            void navigate("/cases");
          }}
          style={{ paddingLeft: 0, marginBottom: 5.6 }}
        >
          <i className="ph ph-arrow-left" style={{ fontSize: 15 }} /> Cases
        </button>
        <p className="sc-text-muted" style={{ fontSize: 13 }}>
          Case {resolvedCaseId} was not found.
        </p>
      </main>
    );
  }

  return (
    <main
      data-screen-label="Case detail"
      style={{
        width: "100%",
        maxWidth: 1440,
        margin: "0 auto",
        padding: 22.4,
        display: "grid",
        gridTemplateColumns: "minmax(0, 1fr) 360px",
        gap: 22.4,
        alignItems: "start",
      }}
    >
      <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 16.8 }}>
        <div>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              void navigate("/dashboard");
            }}
            style={{ paddingLeft: 0, marginBottom: 5.6 }}
          >
            <i className="ph ph-arrow-left" style={{ fontSize: 15 }} /> Dashboard
          </button>
          <div style={{ display: "flex", alignItems: "center", gap: 11.2, flexWrap: "wrap" }}>
            <span className="sc-code" style={{ color: "var(--sc-accent-300)", fontSize: 13 }}>
              {summary.id}
            </span>
            <h4 style={{ margin: 0 }}>{summary.title}</h4>
            <span className={severityTagClass(summary.severity)}>{summary.severity}</span>
            <span className="tag tag-neutral">{summary.stage}</span>
          </div>
          {detail && (
            <div
              className="sc-text-muted"
              style={{ display: "flex", gap: 11.2, fontSize: 12, marginTop: 5.6, flexWrap: "wrap" }}
            >
              <span>Opened {detail.openedAt}</span>
              <span>·</span>
              <span>Lead: {detail.lead}</span>
              <span>·</span>
              <span>{detail.memberCount} members</span>
              {detail.legalHold && (
                <>
                  <span>·</span>
                  <span>Legal hold active</span>
                </>
              )}
            </div>
          )}
        </div>

        <div className="seg" role="tablist">
          <NavLink to={`/cases/${resolvedCaseId}`} end className={segLinkClass}>
            <span>Overview</span>
          </NavLink>
          <NavLink to={`/cases/${resolvedCaseId}/evidence`} className={segLinkClass}>
            <span>Evidence · {evidenceCount}</span>
          </NavLink>
          <NavLink to={`/cases/${resolvedCaseId}/findings`} className={segLinkClass}>
            <span>AI findings · {openFindings}</span>
          </NavLink>
          <NavLink to={`/cases/${resolvedCaseId}/timeline`} className={segLinkClass}>
            <span>Timeline</span>
          </NavLink>
        </div>

        <Outlet
          context={
            {
              caseId: resolvedCaseId,
              detail,
              findings,
              askCopilot: copilot.send,
            } satisfies CaseOutletContext
          }
        />
      </div>

      <aside
        style={{
          position: "sticky",
          top: 78,
          display: "flex",
          flexDirection: "column",
          gap: 11.2,
          borderRadius: 14,
          background: "var(--sc-surface)",
          boxShadow: "var(--sc-shadow-lg)",
          padding: 16.8,
          maxHeight: "calc(100vh - 100px)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8.4 }}>
          <i className="ph-fill ph-sparkle" style={{ fontSize: 16, color: "var(--sc-accent)" }} />
          <span style={{ fontFamily: "var(--sc-font-heading)", fontWeight: 500, fontSize: 15 }}>
            Investigation copilot
          </span>
          <span className="tag tag-neutral" style={{ marginLeft: "auto" }}>
            {resolvedCaseId}
          </span>
        </div>
        <div
          style={{
            flex: 1,
            overflowY: "auto",
            display: "flex",
            flexDirection: "column",
            gap: 11.2,
            paddingRight: 2.8,
          }}
        >
          {copilot.messages.map((m) => (
            <div key={m.id} style={{ display: "flex", flexDirection: "column", gap: 2.8 }}>
              <span
                className="sc-text-muted"
                style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase" }}
              >
                {m.who}
              </span>
              <p
                style={{
                  margin: 0,
                  fontSize: 13,
                  lineHeight: 1.5,
                  padding: "8.4px 11.2px",
                  borderRadius: 8,
                  background:
                    m.who === "Copilot"
                      ? "color-mix(in srgb, #9184d9 12%, transparent)"
                      : "color-mix(in srgb, #e9e9ed 6%, transparent)",
                }}
              >
                {m.text}
              </p>
              {m.cite && (
                <span className="sc-code sc-text-muted" style={{ fontSize: 11 }}>
                  {m.cite}
                </span>
              )}
            </div>
          ))}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 5.6 }}>
          <span
            className="sc-text-muted"
            style={{ fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase" }}
          >
            Suggested next steps
          </span>
          {COPILOT_SUGGESTED_PROMPTS.map((prompt) => (
            <button
              key={prompt}
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                copilot.send(prompt);
              }}
              style={{
                justifyContent: "flex-start",
                textAlign: "left",
                fontSize: 13,
                width: "100%",
              }}
            >
              {prompt}
            </button>
          ))}
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            copilot.send(copilot.draft);
          }}
          style={{ display: "flex", gap: 5.6 }}
        >
          <input
            className="input"
            placeholder="Ask about this case…"
            value={copilot.draft}
            onChange={(e) => {
              copilot.setDraft(e.target.value);
            }}
          />
          <button type="submit" className="btn btn-primary btn-icon" aria-label="Send">
            <i className="ph ph-paper-plane-right" style={{ fontSize: 15 }} />
          </button>
        </form>
        <p className="sc-text-muted" style={{ fontSize: 10, margin: 0 }}>
          Copilot output is a lead, not a determination. Findings require analyst disposition.
        </p>
      </aside>
    </main>
  );
}
