/**
 * Client-side verification of the custody hash chain (CEM §4, api-design.md §5).
 *
 * `api-design.md` §5 commits the custody endpoint to returning each event so the chain is
 * "independently verifiable by the caller". This is that verification: it recomputes every
 * `entry_hash` from the fields the server hashed and confirms each entry names its predecessor.
 * Nothing here trusts the server's own claim of integrity — that is the entire point.
 *
 * **This must mirror `_custody_entry_hash` in the backend's `ingestion/service.py` byte for
 * byte.** Any divergence produces a false "verification failed" on a perfectly intact ledger,
 * which on a legal-custody surface is a serious defect in its own right. The two subtleties that
 * make this non-obvious are documented at `custodyPreimage` and `toPythonIsoformat` below; both
 * were confirmed against the running backend rather than inferred.
 *
 * No external crypto dependency: SHA-256 comes from the platform's Web Crypto.
 */

import type { CustodyEvent } from "../types";

/**
 * The genesis entry's `prev_event_hash`. The column is `NOT NULL`, so the first event carries
 * this sentinel — and the sentinel is the literal string hashed into that entry's own
 * `entry_hash`, which is why it is verified rather than skipped.
 */
export const GENESIS_PREV_HASH = "0".repeat(64);

export type ChainVerification =
  | { status: "pending" }
  /** Nothing to verify — an empty ledger is not a failure. */
  | { status: "idle" }
  | { status: "verified"; count: number }
  | { status: "failed"; reason: string; sequenceNumber: number }
  /** Could not be checked at all. Says nothing about the ledger's integrity. */
  | { status: "unavailable"; reason: string };

/**
 * Convert the wire timestamp to the exact string Python hashed.
 *
 * The server hashes `occurred_at.isoformat()`, which renders UTC as a `+00:00` offset, but
 * Pydantic serialises the same value to JSON with a `Z` suffix (api-design.md §2.3 mandates the
 * `Z` form on the wire). So the received string is *not* the hashed string, and the difference is
 * exactly those six characters.
 *
 * Deliberately a string operation, never a `Date` round-trip: `Date` holds only milliseconds, and
 * these timestamps carry microseconds from a Postgres `timestamptz`. Parsing and re-formatting
 * would silently truncate `.123456` to `.123` and break every hash. The fractional part is
 * already byte-identical between the two representations — six digits when microseconds are
 * non-zero (trailing zeros preserved, e.g. `.100000`), and absent entirely when they are zero —
 * so it is carried across untouched.
 */
export function toPythonIsoformat(wireTimestamp: string): string {
  if (wireTimestamp.endsWith("Z")) {
    return `${wireTimestamp.slice(0, -1)}+00:00`;
  }
  // Already in offset form, or something unexpected. Passed through unchanged rather than
  // guessed at: a wrong guess would fabricate a mismatch and accuse an intact ledger.
  return wireTimestamp;
}

/**
 * Rebuild the exact byte string the backend hashed.
 *
 * The backend calls `json.dumps(..., sort_keys=True, separators=(",", ":"))`, so: no whitespace,
 * and keys in ASCII-sorted order — `event_type`, `evidence_id`, `integrity_hash_at_event`,
 * `occurred_at`, `prev`, `seq`. Note `"event_type"` sorts *before* `"evidence_id"` (`e` < `i` at
 * the third character), which is easy to get backwards by eye.
 *
 * **Two of those keys are abbreviated in the preimage and only there:** the payload field is
 * `prev_event_hash` and `sequence_number`, but the hashed dictionary uses `prev` and `seq`. This
 * mapping is the single most likely thing to get wrong, and getting it wrong fails every entry.
 *
 * The order is written out literally rather than produced by sorting an object at runtime, so the
 * sorted order is visible in review and cannot drift if a field is added.
 *
 * Values go through `JSON.stringify` for escaping. Python's `json.dumps` defaults to
 * `ensure_ascii=True` and would emit `\uXXXX` for non-ASCII where `JSON.stringify` emits the
 * character raw — the two agree here only because every hashed value is ASCII by construction
 * (hex digests, a UUID, an ISO-8601 timestamp, and `event_type`, a closed lowercase enum from
 * CEM §4). A future non-ASCII field in this preimage would need explicit escaping.
 */
export function custodyPreimage(event: CustodyEvent): string {
  return (
    `{"event_type":${JSON.stringify(event.event_type)},` +
    `"evidence_id":${JSON.stringify(event.evidence_id)},` +
    `"integrity_hash_at_event":${JSON.stringify(event.integrity_hash_at_event)},` +
    `"occurred_at":${JSON.stringify(toPythonIsoformat(event.occurred_at))},` +
    `"prev":${JSON.stringify(event.prev_event_hash)},` +
    `"seq":${String(event.sequence_number)}}`
  );
}

/** Lowercase hex, matching Python's `hexdigest()`. */
function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * `crypto.subtle`, or `undefined` where the platform does not provide it.
 *
 * Web Crypto is exposed only in a **secure context**. This is not a hypothetical gap for this
 * product: `frontend-architecture.md` §2's air-gapped profile is served over plain HTTP on a LAN
 * address, which is not a secure context, and `shared/api/client.ts` already probes for the same
 * reason around `crypto.randomUUID`. `globalThis` is narrowed to an optional shape so the check
 * is honest rather than a suppression of a "this is always defined" lint.
 */
function subtleCrypto(): SubtleCrypto | undefined {
  return (globalThis as { crypto?: { subtle?: SubtleCrypto } }).crypto?.subtle;
}

async function sha256Hex(subtle: SubtleCrypto, input: string): Promise<string> {
  // UTF-8 on both sides: Python's `str.encode()` defaults to UTF-8, as does TextEncoder.
  const digest = await subtle.digest("SHA-256", new TextEncoder().encode(input));
  return toHex(digest);
}

/**
 * Recompute and verify the whole ledger.
 *
 * Three independent checks per entry: the recomputed `entry_hash` matches the stored one; the
 * `prev_event_hash` matches the previous entry's `entry_hash` (or the genesis sentinel for the
 * first); and `sequence_number` is contiguous from 1. The sequence check is what catches an entry
 * removed from the *front* of the ledger, which the hashes alone would not — a valid chain
 * starting at sequence 5 is still a valid chain.
 *
 * **Known limit, worth stating plainly:** entries removed from the *end* leave a shorter but
 * internally consistent chain, and no client-side check can detect that. Catching truncation
 * needs an external anchor (ADR-0003's periodic anchoring), not a recomputation. "Verified" here
 * means "this ledger is internally consistent and unaltered", not "this ledger is complete".
 *
 * A copy is sorted by `sequence_number` for the walk. That is computation, not presentation —
 * the ledger is still *displayed* in the server's order, which `api-design.md` §5 requires never
 * be resorted.
 */
export async function verifyCustodyChain(events: CustodyEvent[]): Promise<ChainVerification> {
  if (events.length === 0) {
    return { status: "idle" };
  }

  const subtle = subtleCrypto();
  if (subtle === undefined) {
    return {
      status: "unavailable",
      reason:
        "Web Crypto is not available in this context. Verification requires a secure context (HTTPS or localhost).",
    };
  }

  const ordered = [...events].sort((a, b) => a.sequence_number - b.sequence_number);

  let expectedPrev = GENESIS_PREV_HASH;
  let expectedSequence = 1;

  try {
    for (const event of ordered) {
      if (event.sequence_number !== expectedSequence) {
        return {
          status: "failed",
          sequenceNumber: event.sequence_number,
          reason: `Expected sequence ${String(expectedSequence)} but found ${String(event.sequence_number)} — an entry is missing from the ledger.`,
        };
      }

      if (event.prev_event_hash !== expectedPrev) {
        return {
          status: "failed",
          sequenceNumber: event.sequence_number,
          reason:
            expectedSequence === 1
              ? "The first entry does not carry the genesis hash — the start of the ledger is missing or altered."
              : "This entry does not link to the previous entry's hash — the chain is broken here.",
        };
      }

      const recomputed = await sha256Hex(subtle, custodyPreimage(event));
      if (recomputed !== event.entry_hash) {
        return {
          status: "failed",
          sequenceNumber: event.sequence_number,
          reason: "The recorded hash does not match this entry's contents — the entry was altered.",
        };
      }

      expectedPrev = event.entry_hash;
      expectedSequence += 1;
    }
  } catch {
    // A crypto failure is not evidence of tampering, so it must not be reported as one.
    return { status: "unavailable", reason: "The integrity check could not be completed." };
  }

  return { status: "verified", count: ordered.length };
}
