/**
 * Shared shapes for the console's mock data layer (shared/mock/*).
 *
 * There is no case/finding/evidence/graph/report API yet (apps/server/entrypoints/http has no
 * routes for them — see docs/api-design.md). These types stand in for what a real API response
 * would look like so every consuming feature can swap `shared/mock/*` for React Query hooks later
 * (frontend-architecture.md §10) without changing shape. Nothing here should be treated as an API
 * contract — inventing one is exactly what CLAUDE.md's "never invent an endpoint" rule forbids.
 */

export type Severity = "Critical" | "High" | "Medium" | "Low";

export interface CaseSummary {
  id: string;
  title: string;
  severity: Severity;
  stage: string;
  findingsOpen: number;
  updated: string;
}

export interface CaseDetail {
  id: string;
  title: string;
  severity: Severity;
  stage: string;
  openedAt: string;
  lead: string;
  memberCount: number;
  legalHold: boolean;
  hypothesis: string;
  hypothesisEditedBy: string;
  hypothesisEditedAgo: string;
  overviewStats: { label: string; value: string; note: string }[];
  keyEntities: { label: string; icon: string }[];
}

export type IntegrityStatus = "Verified" | "Pending scan";

export interface EvidenceRow {
  id: string;
  name: string;
  hash: string;
  source: string;
  collected: string;
  integrity: IntegrityStatus;
  custody: string;
}

export type FindingConfidence = "High" | "Medium" | "Low";
export type FindingDisposition = "Confirmed" | "Rejected" | "Escalated";

export interface FindingSeed {
  id: string;
  confidence: FindingConfidence;
  kind: string;
  title: string;
  reasoning: string;
  sources: string[];
}

export interface TimelineEvent {
  when: string;
  what: string;
  detail: string;
  dot: "ok" | "warn" | "idle";
}

export interface EntityFact {
  kind: string;
  note: string;
}

export interface GraphRelation {
  from: string;
  to: string;
  type: string;
}

export interface ReportSection {
  title: string;
  meta: string;
  state: "Drafted" | "Needs review";
}

export interface ExportRecord {
  what: string;
  who: string;
  when: string;
}

export interface CopilotMessage {
  id: string;
  who: string;
  text: string;
  cite?: string;
}
