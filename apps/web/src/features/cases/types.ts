/**
 * Case types, mirroring the backend's `CaseRead` schema
 * (`apps/server/src/sentinelai/modules/case_management/schemas.py`) and `api-design.md` §7.
 *
 * Hand-written rather than generated: there is no OpenAPI type-generation step yet, so this is
 * the one place that must be updated in step with the response schema. Generating these from the
 * server's OpenAPI document is the obvious next hardening step.
 */

/** `case_management.cases.status` — the lifecycle from ADR-0015 / api-design.md §7. */
export type CaseStatus = "open" | "closed" | "archived";

/**
 * Longest `title` the backend accepts (`CaseCreate.title` is `Field(min_length=1,
 * max_length=200)`). Enforced client-side purely as UX — §15 is explicit that the server stays
 * the authority, so this shortens the feedback loop rather than replacing validation.
 */
export const CASE_TITLE_MAX_LENGTH = 200;

/**
 * The `POST /api/v1/cases` request body, mirroring the backend's `CaseCreate`.
 *
 * Exactly two fields: the endpoint takes no status, owner, or classification input. `status`
 * starts at `open` and `owning_user_id` is taken from the authenticated caller — both are the
 * server's to decide, so neither belongs on this form.
 */
export interface CreateCaseDTO {
  title: string;
  description?: string;
}

/**
 * One row of `GET /api/v1/cases/{case_id}/evidence`, mirroring the backend's
 * `CaseEvidenceLinkRead`.
 *
 * This is the **link**, not the evidence. `case_management` owns only the join table:
 * `ingestion.evidence` belongs to another module and `database-design.md` forbids a cross-schema
 * foreign key, so the endpoint can return the association and its provenance but genuinely cannot
 * return the evidence's title, category, artifact type, or integrity status. Rendering those
 * requires resolving each `evidence_id` against `GET /api/v1/evidence/{evidence_id}`.
 */
export interface CaseEvidenceLink {
  link_id: string;
  case_id: string;
  evidence_id: string;
  linked_by_user_id: string;
  linked_at: string;
}

/** One case, exactly as `GET /api/v1/cases` returns it inside the envelope's `data`. */
export interface Case {
  case_id: string;
  title: string;
  description: string | null;
  /**
   * `string`, not `CaseStatus`, deliberately: a status from a newer server must render rather
   * than crash the list. `CaseStatus` stays the authority on the *known* values, so anything
   * handling them exhaustively (styling, filters) still gets compile-time coverage.
   */
  status: string;
  owning_user_id: string;
  created_at: string;
  closed_at: string | null;
}
