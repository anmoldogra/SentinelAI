import { useOutletContext } from "react-router-dom";

import type { CaseOutletContext } from "@/features/cases/routes/CaseLayout";
import { dotColor } from "@/shared/console/severity";
import { TIMELINE_BY_CASE } from "@/shared/mock/timeline";

/** Timeline tab (frontend-architecture.md §28) — aggregated across evidence/custody/status/finding events. */
export function CaseTimelinePage() {
  const { caseId } = useOutletContext<CaseOutletContext>();
  const events = TIMELINE_BY_CASE[caseId] ?? [];

  if (events.length === 0) {
    return (
      <p className="sc-text-muted" style={{ fontSize: 13 }}>
        No timeline events have been seeded for this case yet.
      </p>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {events.map((t) => (
        <div
          key={`${t.when}-${t.what}`}
          style={{
            display: "grid",
            gridTemplateColumns: "132px 16px minmax(0, 1fr)",
            gap: 11.2,
            padding: "11.2px 0",
            boxShadow: "inset 0 -1px 0 color-mix(in srgb, var(--sc-text) 8%, transparent)",
          }}
        >
          <span className="sc-text-muted sc-code" style={{ fontSize: 12 }}>
            {t.when}
          </span>
          <i
            className="ph-fill ph-circle"
            style={{ fontSize: 8, color: dotColor(t.dot), marginTop: 6 }}
          />
          <div style={{ display: "flex", flexDirection: "column", gap: 2.8 }}>
            <span style={{ fontSize: 14 }}>{t.what}</span>
            <span className="sc-text-muted" style={{ fontSize: 12 }}>
              {t.detail}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
