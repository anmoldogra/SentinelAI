export interface SearchableCase {
  id: string;
  label: string;
  meta: string;
}

export interface SearchableEvidence {
  id: string;
  label: string;
  meta: string;
  caseId: string;
}

export interface SearchableEntity {
  id: string;
  label: string;
  meta: string;
  caseId: string;
}

/** Cross-resource lookup data backing the ⌘K palette (frontend-architecture.md §29, §37). */
export const SEARCHABLE_CASES: SearchableCase[] = [
  {
    id: "CASE-2041",
    label: "CASE-2041 · Ransomware deployment",
    meta: "Northline Logistics · Critical",
  },
  { id: "CASE-2038", label: "CASE-2038 · Payroll BEC", meta: "Wire diverted to mule account" },
];

export const SEARCHABLE_EVIDENCE: SearchableEvidence[] = [
  {
    id: "EV-0298",
    label: "EV-0298 · NL-FS02 disk image",
    meta: "Forensic image · verified",
    caseId: "CASE-2041",
  },
];

export const SEARCHABLE_ENTITIES: SearchableEntity[] = [
  {
    id: "185.220.101.44",
    label: "185.220.101.44",
    meta: "IP address · exfil destination",
    caseId: "CASE-2041",
  },
  { id: "m.hale", label: "m.hale", meta: "Account · initial access", caseId: "CASE-2041" },
];
