import type { FindingSeed } from "./types";

/**
 * Mock stand-in for the review queue's underlying data (frontend-architecture.md §24 — a real
 * implementation reads `GET /relationships?status=proposed` scoped to the case). Disposition
 * state (confirmed/rejected/escalated, and the non-optimistic pending state) lives with the
 * consuming case route, not here — this file is the immutable seed only.
 */
export const FINDINGS_BY_CASE: Record<string, FindingSeed[]> = {
  "CASE-2041": [
    {
      id: "F-0031",
      confidence: "High",
      kind: "Correlation",
      title: "VPN account m.hale reused from a residential proxy 9 hours before first encryption",
      reasoning:
        "The same session fingerprint appears in the VPN concentrator log and in the endpoint auth events on NL-FS02, from an ASN with no prior history for this tenant. Interval between login and first SMB write is 31 minutes.",
      sources: ["EV-0114", "EV-0231", "EV-0298"],
    },
    {
      id: "F-0032",
      confidence: "High",
      kind: "Threat intel",
      title: "Ransom-note wallet matches a known affiliate crew tracked as ASHEN-DRIFT",
      reasoning:
        "Wallet bc1q…7f4e appears in two threat-intel feeds and one OSINT paste dated 16 Aug, both attributed to the same affiliate. Note template matches the family's 2026 variant.",
      sources: ["EV-0402", "OSINT-0042"],
    },
    {
      id: "F-0033",
      confidence: "Medium",
      kind: "Anomaly",
      title: "3.1 GB outbound to 185.220.101.44 in the 40 minutes before encryption",
      reasoning:
        "Egress volume for NL-FS02 exceeds its 30-day baseline by 22×, to a destination first seen during this incident. TLS SNI absent; port 443 with non-browser JA3.",
      sources: ["EV-0301", "EV-0377"],
    },
    {
      id: "F-0034",
      confidence: "Low",
      kind: "Lead",
      title: "Second service account may share the initial-access credential",
      reasoning:
        "svc-backup authenticated from the same subnet 6 minutes after m.hale, but no artifact ties it to the intrusion yet. Offered as a lead for manual review.",
      sources: ["EV-0244"],
    },
  ],
};
