/**
 * Mock stand-in for the dashboard's independently-fetched widgets (frontend-architecture.md §23).
 * Each widget is its own would-be query, so — matching a real backend, where a widget's count
 * would come from its own endpoint rather than from another screen's client-side state — nothing
 * here is derived from case/finding mock state at render time.
 */

export interface StatCard {
  label: string;
  value: string;
  note: string;
}

export const DASHBOARD_STATS: StatCard[] = [
  { label: "Open cases", value: "6", note: "2 critical" },
  { label: "Findings queued", value: "14", note: "oldest 14h" },
  { label: "Evidence today", value: "1,204", note: "all hashes verified" },
  { label: "SLA at risk", value: "1", note: "CASE-2038 · 3h" },
];

export interface QueueItem {
  caseId: string;
  confidence: "High" | "Medium" | "Low";
  age: string;
  title: string;
  basis: string;
}

export const DASHBOARD_QUEUE: QueueItem[] = [
  {
    caseId: "CASE-2041",
    confidence: "High",
    age: "14h",
    title: "VPN account reused from a residential proxy before first encryption",
    basis: "EV-0114 · EV-0231 · EV-0298",
  },
  {
    caseId: "CASE-2038",
    confidence: "High",
    age: "6h",
    title: "Reply-to domain registered 3 days before the wire request",
    basis: "EV-0521 · OSINT-0067",
  },
  {
    caseId: "CASE-2034",
    confidence: "Medium",
    age: "3h",
    title: "Archive staged in a folder excluded from DLP scanning",
    basis: "EV-0603",
  },
];

export interface ConnectorStatus {
  name: string;
  detail: string;
  dot: "ok" | "warn" | "idle";
}

export const CONNECTORS: ConnectorStatus[] = [
  { name: "EDR · endpoint telemetry", detail: "live · 2s lag", dot: "ok" },
  { name: "Forensic image intake", detail: "live · 1 job", dot: "ok" },
  { name: "Threat intel feeds", detail: "degraded · 2 of 5", dot: "warn" },
  { name: "OSINT collectors", detail: "degraded · rate limited", dot: "warn" },
  { name: "Social monitoring", detail: "idle", dot: "idle" },
];

export interface AuditEntry {
  what: string;
  who: string;
  when: string;
}

export const RECENT_AUDIT: AuditEntry[] = [
  { what: "F-0029 confirmed on CASE-2038", who: "d.mensah", when: "12m ago" },
  { what: "EV-0402 hash re-verified", who: "system", when: "24m ago" },
  { what: "Legal hold applied to CASE-2041", who: "a.varga", when: "1h ago" },
  { what: "Report draft exported (PDF)", who: "r.okafor", when: "3h ago" },
];
