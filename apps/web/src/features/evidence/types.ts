/**
 * Evidence types, mirroring the backend's `EvidenceRead`
 * (`apps/server/src/sentinelai/modules/ingestion/schemas.py`) and `api-design.md` §5.
 *
 * Hand-written rather than generated: there is no OpenAPI type-generation step yet, so this is
 * the one place that must be updated in step with the response schema.
 */

/**
 * `ingestion.evidence.integrity_verification_status` (ADR-0008 §3).
 *
 * The complete set the service actually writes: `pending` and `not_applicable` at ingest
 * (depending on whether the evidence carries a payload at all), then `verified` or `failed` once
 * the server recomputes the hash and compares it to the recorded one. The column is nullable, so
 * `null` is a fourth state meaning "never recorded".
 */
export type IntegrityVerificationStatus = "pending" | "verified" | "failed" | "not_applicable";

/**
 * One entry in an evidence item's chain-of-custody ledger, mirroring the backend's
 * `CustodyEventRead` — now the complete custody event CEM §4 defines.
 *
 * The response schema was expanded additively to return every field of the entry-hash preimage,
 * so `api-design.md` §5's commitment that the chain is "independently verifiable by the caller"
 * is now actually satisfiable here. The server computes `entry_hash` over
 * `{prev, evidence_id, seq, event_type, integrity_hash_at_event, occurred_at}`, and this type
 * carries all six of those inputs alongside the ledger's descriptive fields.
 *
 * **Nullable, not optional.** `actor_user_id`, `actor_role`, `authority_ref`, and `notes` are
 * typed `str | None` on the backend, and Pydantic emits the key with a `null` value rather than
 * omitting it. Declaring them `?:` under `exactOptionalPropertyTypes` would describe a shape the
 * API never sends — an absent key and a null value are different things here, and only the
 * latter occurs.
 */
export interface CustodyEvent {
  custody_event_id: string;
  evidence_id: string;
  /** Monotonically increasing per `evidence_id` (CEM §4). The ledger's canonical order. */
  sequence_number: number;
  event_type: string;
  occurred_at: string;
  /** Null for events with no human actor. */
  actor_user_id: string | null;
  /** The actor's role *at the time of the action*, retained even if the role later changes. */
  actor_role: string | null;
  /** Legal authority for this specific action, where distinct from the evidence's own. */
  authority_ref: string | null;
  /**
   * Payload hash recomputed at this event — proves what was accessed, exported, or analyzed
   * matched the original (CEM §4). Also one of the six `entry_hash` preimage inputs.
   */
  integrity_hash_at_event: string;
  /**
   * The preceding entry's `entry_hash`. The genesis event carries the all-zero sentinel rather
   * than null: that sentinel is the literal value hashed into its own `entry_hash`, so it is what
   * a verifying client must feed back in to reproduce the genesis preimage (CEM §4).
   */
  prev_event_hash: string;
  /** Computed over this entry's fields plus `prev_event_hash` (CEM §4). */
  entry_hash: string;
  /** Free text, e.g. the reason for an access event. */
  notes: string | null;
}

/**
 * The CEM schema version every upload declares.
 *
 * Matches what Alembic revision `202608300002_ingestion_seed` wrote into
 * `ingestion.attribute_schema_registry`. The backend rejects any
 * `(schema_version, category, artifact_type)` triple that is not registered there, so this
 * constant and that migration have to move together.
 */
export const EVIDENCE_SCHEMA_VERSION = "1.0.0";

/** `EvidenceCreate.title`'s `max_length`. Mirrored so the field can stop typing at the limit. */
export const EVIDENCE_TITLE_MAX_LENGTH = 200;

/** One registered `(category, artifact_type)` pair, with the label the picker shows for it. */
export interface BaselineEvidenceSchema {
  category: string;
  artifact_type: string;
  categoryLabel: string;
  artifactTypeLabel: string;
}

/**
 * The registered artifact types an analyst may upload through the console.
 *
 * **This is a mirror, not the source of truth.** The authority is the registry table, exposed by
 * `GET /api/v1/evidence/attribute-schemas`. Reading it at runtime is the right long-term shape and
 * is a deliberate follow-up; hardcoding the five seeded triples keeps this increment to the upload
 * flow itself. The cost of the mirror is precise: a triple registered on the server but missing
 * here is simply un-uploadable from the UI, and a triple listed here but *not* registered on the
 * server is rejected at finalize with a `VALIDATION_FAILED` naming `schema_version` — a clear
 * server error, never a silent bad write.
 */
export const BASELINE_EVIDENCE_SCHEMAS: readonly BaselineEvidenceSchema[] = [
  {
    category: "drone_iot",
    artifact_type: "cfid_log",
    categoryLabel: "Drone / IoT",
    artifactTypeLabel: "CFID flight log",
  },
  {
    category: "drone_iot",
    artifact_type: "datcon_log",
    categoryLabel: "Drone / IoT",
    artifactTypeLabel: "DATCON flight log",
  },
  {
    category: "mobile_forensics",
    artifact_type: "oxygen_extraction",
    categoryLabel: "Mobile forensics",
    artifactTypeLabel: "Oxygen device extraction",
  },
  {
    category: "cloud_evidence",
    artifact_type: "oxygen_cloud_extraction",
    categoryLabel: "Cloud evidence",
    artifactTypeLabel: "Oxygen cloud extraction",
  },
  {
    category: "digital_forensics",
    artifact_type: "forensic_image",
    categoryLabel: "Digital forensics",
    artifactTypeLabel: "Forensic image",
  },
];

/**
 * Categories for which the backend requires `legal_authority_ref` (CEM §13, mirrored in
 * `ingestion/service.py`'s `_LEGAL_AUTHORITY_REQUIRED`).
 *
 * Mirrored so the form can ask for it *before* the server rejects the submission — client
 * validation is UX only (frontend-architecture.md §15); the server stays the authority. Note
 * `drone_iot` is deliberately absent: requiring an authority reference there would block a
 * legitimate upload the API would have accepted.
 */
export const LEGAL_AUTHORITY_REQUIRED_CATEGORIES: readonly string[] = [
  "digital_forensics",
  "mobile_forensics",
  "social_media_intelligence",
  "cloud_evidence",
];

/**
 * The sentinel the backend accepts in place of a real authority reference for public-source
 * material (`_PUBLIC_SOURCE_SENTINEL`). Offered as a hint, never substituted automatically — the
 * analyst asserts it, the UI does not assert it for them.
 */
export const PUBLIC_SOURCE_SENTINEL = "public_source_no_authority_required";

/** One evidence item, exactly as `GET /api/v1/evidence/{evidence_id}` returns it in `data`. */
export interface Evidence {
  evidence_id: string;
  schema_version: string;
  category: string;
  artifact_type: string;
  title: string;
  description: string | null;
  /**
   * `string`, not a union, deliberately — the same reasoning as `Case.status`: a status from a
   * newer server must render rather than crash the table. The union types above stay the
   * authority on the *known* values, so anything handling them exhaustively still gets
   * compile-time coverage.
   */
  status: string;
  /**
   * A JSON **string**, not a number. The backend types this `Decimal`, and Pydantic v2 serialises
   * `Decimal` to a string in JSON mode to preserve precision — there is no `field_serializer`
   * overriding that anywhere in the module. Parse it before comparing numerically; never assume
   * arithmetic works on it as-is.
   */
  confidence: string;
  collected_at: string;
  ingested_at: string;
  /** Nullable in the column, so absence is a real state — see `IntegrityVerificationStatus`. */
  integrity_verification_status: string | null;
  legal_hold: boolean;
}
