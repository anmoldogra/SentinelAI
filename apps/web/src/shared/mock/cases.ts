import type { CaseDetail, CaseSummary } from "./types";

/** Mock stand-in for `GET /cases` (frontend-architecture.md §25). */
export const CASE_SUMMARIES: CaseSummary[] = [
  {
    id: "CASE-2041",
    title: "Ransomware deployment · Northline Logistics",
    severity: "Critical",
    stage: "Containment",
    findingsOpen: 4,
    updated: "8m ago",
  },
  {
    id: "CASE-2038",
    title: "Payroll BEC · wire diverted to mule account",
    severity: "High",
    stage: "Analysis",
    findingsOpen: 6,
    updated: "51m ago",
  },
  {
    id: "CASE-2034",
    title: "Insider exfiltration · design files to personal cloud",
    severity: "High",
    stage: "Analysis",
    findingsOpen: 2,
    updated: "2h ago",
  },
  {
    id: "CASE-2029",
    title: "Credential stuffing against customer portal",
    severity: "Medium",
    stage: "Monitoring",
    findingsOpen: 1,
    updated: "5h ago",
  },
  {
    id: "CASE-2021",
    title: "Vendor account takeover · invoice fraud",
    severity: "Medium",
    stage: "Reporting",
    findingsOpen: 0,
    updated: "1d ago",
  },
  {
    id: "CASE-2014",
    title: "Phishing kit hosted on partner subdomain",
    severity: "Low",
    stage: "Closing",
    findingsOpen: 1,
    updated: "2d ago",
  },
];

/**
 * Only CASE-2041 (the case the design was built against) has full mock detail. Every other case
 * is a real, navigable row with no seeded detail yet — routes render an honest empty state rather
 * than silently reusing CASE-2041's data.
 */
export const CASE_DETAILS: Record<string, CaseDetail> = {
  "CASE-2041": {
    id: "CASE-2041",
    title: "Ransomware deployment · Northline Logistics",
    severity: "Critical",
    stage: "Containment",
    openedAt: "27 Aug 06:14",
    lead: "r.okafor",
    memberCount: 4,
    legalHold: true,
    hypothesis:
      "Initial access via a compromised VPN account on 26 Aug, lateral movement to the file cluster over SMB, exfiltration to 185.220.101.44 before encryption of NL-FS02. Two OSINT hits tie the wallet address to a known affiliate crew.",
    hypothesisEditedBy: "r.okafor",
    hypothesisEditedAgo: "2h ago",
    overviewStats: [
      { label: "Evidence items", value: "812", note: "6 sources" },
      { label: "Entities", value: "147", note: "23 linked to intrusion" },
      { label: "Custody events", value: "1,908", note: "unbroken" },
    ],
    keyEntities: [
      { label: "m.hale (VPN account)", icon: "ph-user" },
      { label: "NL-FS02", icon: "ph-desktop" },
      { label: "185.220.101.44", icon: "ph-globe" },
      { label: "bc1q…7f4e", icon: "ph-currency-btc" },
      { label: "ASHEN-DRIFT", icon: "ph-crosshair" },
      { label: "svc-backup", icon: "ph-user-gear" },
      { label: "note.readme.txt", icon: "ph-file-text" },
      { label: "AS14061", icon: "ph-tree-structure" },
    ],
  },
};

export function findCaseSummary(caseId: string): CaseSummary | undefined {
  return CASE_SUMMARIES.find((c) => c.id === caseId);
}
