import type { ExportRecord, ReportSection } from "./types";

/** Mock stand-in for `GET /cases/{id}/reports` (frontend-architecture.md §25). */
export const REPORT_SECTIONS_BY_CASE: Record<string, ReportSection[]> = {
  "CASE-2041": [
    { title: "Executive summary", meta: "1 page", state: "Drafted" },
    { title: "Timeline of events", meta: "6 events", state: "Drafted" },
    { title: "Evidence manifest", meta: "812 items · hashes", state: "Drafted" },
    { title: "Findings and dispositions", meta: "", state: "Needs review" },
    { title: "Containment and remediation", meta: "3 actions", state: "Drafted" },
    { title: "Chain of custody appendix", meta: "1,908 events", state: "Drafted" },
  ],
};

export const EXPORT_HISTORY_BY_CASE: Record<string, ExportRecord[]> = {
  "CASE-2041": [
    { what: "Draft v2 · PDF · internal", who: "r.okafor", when: "3h ago" },
    { what: "Evidence manifest · JSON", who: "d.mensah", when: "Yesterday" },
    { what: "Draft v1 · PDF · counsel", who: "a.varga", when: "29 Aug" },
  ],
};
