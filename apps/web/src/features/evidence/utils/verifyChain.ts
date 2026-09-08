/**
 * Client-side verification of the custody hash chain (CEM §4, api-design.md §5, ADR-0003).
 *
 * `api-design.md` §5 commits the custody endpoint to returning each event so the chain is
 * "independently verifiable by the caller". This is that verification: it recomputes every
 * `entry_hash` from the fields the server hashed and confirms each entry names its predecessor.
 * Nothing here trusts the server's own claim of integrity — that is the entire point.
 *
 * **This must mirror `_custody_entry_hash` in the backend's `ingestion/service.py`.** Any
 * divergence produces a false "verification failed" on a perfectly intact ledger, which on a
 * legal-custody surface is a serious defect in its own right. Wave 1.2 made that mirroring far
 * less fragile than it was:
 *
 * - The preimage is now built with a shared **RFC 8785 (JCS)** encoder (`shared/crypto`) instead
 *   of a hand-written string template that had to reproduce Python's `json.dumps` key order by
 *   eye. Field order is now computed, not transcribed.
 * - The hashed timestamp is now the **wire form** the API actually sends (`...Z`). It used to be
 *   Python's `isoformat()` (`...+00:00`), so the string the server hashed was not the string the
 *   client received and this file had to translate six characters back. That translation, and the
 *   microsecond-truncation hazard that came with it, are gone.
 * - The preimage covers **every** persisted column of the entry, so the attribution fields —
 *   who acted, in what role, under what authority — are now bound to the hash. Before Wave 1.2
 *   they were not, and this verifier would have happily confirmed a ledger whose custodian had
 *   been rewritten.
 *
 * No external crypto dependency: SHA-256 comes from the platform's Web Crypto.
 */

import { canonicalJson, type JsonValue } from "../../../shared/crypto/canonicalJson";
import type { CustodyEvent } from "../types";

/**
 * The genesis entry's `prev_event_hash`. The column is `NOT NULL`, so the first event carries
 * this sentinel — and the sentinel is the literal string hashed into that entry's own
 * `entry_hash`, which is why it is verified rather than skipped.
 */
export const GENESIS_PREV_HASH = "0".repeat(64);

/** The only preimage version this client knows how to rebuild (ADR-0003 §5). */
export const SUPPORTED_PREIMAGE_VERSION = 1;

export type ChainVerification =
  | { status: "pending" }
  /** Nothing to verify — an empty ledger is not a failure. */
  | { status: "idle" }
  | { status: "verified"; count: number }
  /**
   * The chain links and sequence are intact, but some entries predate the complete preimage
   * (ADR-0003 §2) and cannot be recomputed from what the API returns. Distinct from both
   * "verified" and "failed" on purpose — see `verifyCustodyChain`.
   */
  | { status: "partial"; verifiedCount: number; unverifiableCount: number }
  | { status: "failed"; reason: string; sequenceNumber: number }
  /** Could not be checked at all. Says nothing about the ledger's integrity. */
  | { status: "unavailable"; reason: string };

/**
 * Rebuild the exact structure the backend hashed.
 *
 * Mirrors `_custody_entry_hash`, which passes these eleven fields to `compute_entry_hash`; that
 * helper then injects `hash_algo` and `preimage_version` before canonicalizing. Those two are
 * inside the hash rather than beside it as a **downgrade defense**: if the version that says how
 * to verify an entry were merely stored next to it, an attacker could rewrite the entry under the
 * old partial format and reset the version to make it verify under the weaker rules.
 *
 * Two keys are abbreviated in the preimage and only there: the columns are `prev_event_hash` and
 * `sequence_number`, but the hashed object uses `prev` and `seq`.
 *
 * Key order is not written out here — `canonicalJson` sorts, exactly as the backend's JCS encoder
 * does — so adding a field cannot silently put the two implementations out of order.
 */
export function custodyPreimage(event: CustodyEvent): JsonValue {
  return {
    prev: event.prev_event_hash,
    custody_event_id: event.custody_event_id,
    evidence_id: event.evidence_id,
    seq: event.sequence_number,
    event_type: event.event_type,
    occurred_at: event.occurred_at,
    actor_user_id: event.actor_user_id,
    actor_role: event.actor_role,
    authority_ref: event.authority_ref,
    notes: event.notes,
    integrity_hash_at_event: event.integrity_hash_at_event,
    hash_algo: event.hash_algo,
    preimage_version: event.preimage_version,
  };
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
  // UTF-8 on both sides: Python's `canonicalize()` returns UTF-8 bytes, as does TextEncoder.
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
 * **Mixed-format chains.** An entry written before Wave 1.2 carries `preimage_version: null`. It
 * was hashed over a partial field set with a non-canonical encoder and cannot be rebuilt from
 * this payload at all. Such an entry is **skipped for recomputation but still checked for
 * linkage and sequence**, and the result degrades to `partial`. Reporting it as `failed` would
 * accuse an intact ledger; reporting it as `verified` would claim a binding that was never made.
 * Neither is true, so neither is said. The same applies to any future version this build does not
 * know: an old client must not declare a newer entry forged.
 *
 * **Known limit, worth stating plainly:** entries removed from the *end* leave a shorter but
 * internally consistent chain, and no client-side check can detect that. Catching truncation
 * needs an external anchor (ADR-0003 §3, Wave 1.3), not a recomputation. And because entries are
 * hashed but not yet *signed* (ADR-0003 §1 — `signature` is still null), a writer who can rewrite
 * the whole chain can still produce one that verifies here. "Verified" means "this ledger is
 * internally consistent and unaltered by anyone who could not recompute it", not "this ledger is
 * complete" and not yet "this ledger is authentic".
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
  let verifiedCount = 0;
  let unverifiableCount = 0;

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

      if (event.preimage_version === SUPPORTED_PREIMAGE_VERSION) {
        const recomputed = await sha256Hex(subtle, canonicalJson(custodyPreimage(event)));
        if (recomputed !== event.entry_hash) {
          return {
            status: "failed",
            sequenceNumber: event.sequence_number,
            reason:
              "The recorded hash does not match this entry's contents — the entry was altered.",
          };
        }
        verifiedCount += 1;
      } else {
        unverifiableCount += 1;
      }

      expectedPrev = event.entry_hash;
      expectedSequence += 1;
    }
  } catch {
    // A crypto or encoding failure is not evidence of tampering, so it must not be reported as
    // one. This includes a value the canonicalizer refuses.
    return { status: "unavailable", reason: "The integrity check could not be completed." };
  }

  if (unverifiableCount > 0) {
    return { status: "partial", verifiedCount, unverifiableCount };
  }
  return { status: "verified", count: verifiedCount };
}
