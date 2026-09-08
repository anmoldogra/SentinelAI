/**
 * The `evidence` feature's **public surface** — the only module other features may import from.
 *
 * This is the frontend analogue of each backend module's `public.py` (under
 * `apps/server/src/sentinelai/modules/`), which `CLAUDE.md`'s rule 7 already mandates on the
 * backend: a module's internals (`models.py`,
 * `repository.py`) are private, and cross-module code goes through a declared interface.
 * `frontend-architecture.md` §3 states the frontend mirrors that same discipline, so it applies
 * here in the same shape.
 *
 * **The rule:** `features/cases`, `features/investigation`, and anything else that needs evidence
 * imports from `@/features/evidence/public` and nothing deeper. Reaching past this file into
 * `../types` or the `../api` folder from another feature is the violation §3's anti-pattern list
 * names — it is what makes this feature's internals safe to reorganise, because only the exports
 * below are load-bearing outside it.
 *
 * Deliberately narrow. Only what another feature genuinely needs to *resolve and display* an
 * evidence reference is exported; the raw fetcher stays internal so callers cannot bypass the
 * query cache that makes resolution cheap.
 */

export type { Evidence, IntegrityVerificationStatus, BaselineEvidenceSchema } from "./types";
/**
 * The registered upload targets, and the two CEM §13 rules a form needs to ask for the right
 * fields. Exported as data rather than as a "which fields are required" helper: the caller is
 * building a form, and what it needs is the vocabulary, not a validator.
 */
export {
  BASELINE_EVIDENCE_SCHEMAS,
  EVIDENCE_SCHEMA_VERSION,
  EVIDENCE_TITLE_MAX_LENGTH,
  LEGAL_AUTHORITY_REQUIRED_CATEGORIES,
  PUBLIC_SOURCE_SENTINEL,
} from "./types";
export { useEvidenceItem, evidenceQueryKeys } from "./api/useEvidenceItem";
/**
 * Browsing the evidence store is a legitimate cross-feature need — a case links evidence, and an
 * investigation will too — so the list hook joins the surface. The fetcher behind it stays
 * internal: callers go through the hook so every consumer shares one cache entry.
 */
export { useEvidence } from "./api/useEvidence";
/**
 * Uploading joins the surface for the same reason browsing did: a case is where an analyst adds an
 * artifact, and `features/cases` cannot reach the hook any other way. The four-step orchestration
 * stays inside this feature — callers get one mutation and a phase, never the individual steps,
 * so no other feature can perform half an upload.
 */
export {
  useUploadEvidence,
  type UploadEvidenceInput,
  type UploadPhase,
} from "./api/useUploadEvidence";
