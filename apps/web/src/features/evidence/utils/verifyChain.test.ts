/**
 * Cross-language lock on the custody hash chain (CEM §4, api-design.md §5).
 *
 * **What this suite is actually for.** `verifyChain.ts` reimplements, in TypeScript, a hash the
 * Python backend computes in `ingestion/service.py::_custody_entry_hash`. Nothing in the type
 * system connects the two. If someone renames a preimage key, reorders the dictionary, or changes
 * how the timestamp is rendered on either side, the code still compiles, still lints, still
 * builds — and every intact custody ledger in the product silently starts reporting
 * **"verification failed"**, telling analysts their evidence was tampered with.
 *
 * That is the failure this file exists to make impossible. The vectors below are not invented:
 * they are the literal output of the backend's own hashing function, captured by running it, and
 * committed here so a divergence on either side fails loudly and immediately.
 *
 * **Regenerating the vector** (only when the preimage contract *intentionally* changes — and then
 * `docs/canonical-evidence-model.md` §4 must change in the same commit):
 *
 * ```
 * cd apps/server && python -c "
 * import sys, json, hashlib
 * sys.path.insert(0,'src')
 * from datetime import datetime, UTC, timedelta
 * from uuid import UUID
 * evid = UUID('1f3b2e2a-0000-4000-8000-000000000001')
 * prev='0'*64
 * base = datetime(2026,6,2,14,3,0,tzinfo=UTC)
 * for i,(et,micro) in enumerate([('collected',123456),('ingested',0),('accessed',100000)], start=1):
 *     occ=(base+timedelta(seconds=i)).replace(microsecond=micro)
 *     ih=hashlib.sha256(('payload%d'%i).encode()).hexdigest()
 *     pre=json.dumps({'prev':prev,'evidence_id':str(evid),'seq':i,'event_type':et,
 *         'integrity_hash_at_event':ih,'occurred_at':occ.isoformat()},
 *         sort_keys=True,separators=(',',':'))
 *     eh=hashlib.sha256(pre.encode()).hexdigest()
 *     print(pre); print(eh)
 *     prev=eh
 * "
 * ```
 *
 * **Why these three entries specifically.** The timestamps deliberately cover every microsecond
 * shape Python's `isoformat()` can emit, because that is where a naive implementation breaks:
 * `.123456` (six digits), none at all (microsecond zero — Python omits the fraction entirely),
 * and `.100000` (trailing zeros preserved, which a `Date` round-trip or a zero-trim would
 * destroy). A suite that only tested one shape would pass while the other two silently failed in
 * production.
 */

import { describe, expect, it } from "vitest";

import type { CustodyEvent } from "../types";
import {
  custodyPreimage,
  GENESIS_PREV_HASH,
  toPythonIsoformat,
  verifyCustodyChain,
} from "./verifyChain";

const EVIDENCE_ID = "1f3b2e2a-0000-4000-8000-000000000001";

/**
 * The canonical ledger, exactly as the backend serialises it on the wire — note the `Z` suffix,
 * which is *not* what gets hashed (Python hashes the `+00:00` form). That mismatch is the whole
 * reason `toPythonIsoformat` exists, so the vector must carry the wire form to exercise it.
 */
const CANONICAL_CHAIN: readonly CustodyEvent[] = [
  {
    custody_event_id: "00000000-0000-0000-0000-000000000001",
    evidence_id: EVIDENCE_ID,
    sequence_number: 1,
    event_type: "collected",
    occurred_at: "2026-06-02T14:03:01.123456Z",
    actor_user_id: null,
    actor_role: "forensic_examiner",
    authority_ref: null,
    integrity_hash_at_event: "75f3ae1057c197610e33633c630a4e0000dd997f095592bb90cf797202a92b07",
    prev_event_hash: "0000000000000000000000000000000000000000000000000000000000000000",
    entry_hash: "188063861e9df2d57f6765bba07dd85c147b3231b4b5e963c2893aeb6924e863",
    notes: null,
  },
  {
    custody_event_id: "00000000-0000-0000-0000-000000000002",
    evidence_id: EVIDENCE_ID,
    sequence_number: 2,
    event_type: "ingested",
    occurred_at: "2026-06-02T14:03:02Z",
    actor_user_id: null,
    actor_role: "forensic_examiner",
    authority_ref: null,
    integrity_hash_at_event: "b96b2ae937a0d587b9890dbad2e7e98d5e9898ed940ec10a7518a14d4fb4b60c",
    prev_event_hash: "188063861e9df2d57f6765bba07dd85c147b3231b4b5e963c2893aeb6924e863",
    entry_hash: "c1c1ebd956c3928840f9f8a3e507c91bf9386cfa20e22aa1a5cacdb18edfc40f",
    notes: null,
  },
  {
    custody_event_id: "00000000-0000-0000-0000-000000000003",
    evidence_id: EVIDENCE_ID,
    sequence_number: 3,
    event_type: "accessed",
    occurred_at: "2026-06-02T14:03:03.100000Z",
    actor_user_id: null,
    actor_role: "forensic_examiner",
    authority_ref: null,
    integrity_hash_at_event: "b4f3bdff83fbf2030f6e60c0ebf5f184946b84855482e2e02b43207f4ced1eb7",
    prev_event_hash: "c1c1ebd956c3928840f9f8a3e507c91bf9386cfa20e22aa1a5cacdb18edfc40f",
    entry_hash: "b4409b1501f680102771155368e2a86a9f2157829de4f4c9fadf79056bab9d7b",
    notes: null,
  },
];

/** Python's `json.dumps(..., sort_keys=True, separators=(",", ":"))` output, verbatim. */
const CANONICAL_PREIMAGES: readonly string[] = [
  '{"event_type":"collected","evidence_id":"1f3b2e2a-0000-4000-8000-000000000001","integrity_hash_at_event":"75f3ae1057c197610e33633c630a4e0000dd997f095592bb90cf797202a92b07","occurred_at":"2026-06-02T14:03:01.123456+00:00","prev":"0000000000000000000000000000000000000000000000000000000000000000","seq":1}',
  '{"event_type":"ingested","evidence_id":"1f3b2e2a-0000-4000-8000-000000000001","integrity_hash_at_event":"b96b2ae937a0d587b9890dbad2e7e98d5e9898ed940ec10a7518a14d4fb4b60c","occurred_at":"2026-06-02T14:03:02+00:00","prev":"188063861e9df2d57f6765bba07dd85c147b3231b4b5e963c2893aeb6924e863","seq":2}',
  '{"event_type":"accessed","evidence_id":"1f3b2e2a-0000-4000-8000-000000000001","integrity_hash_at_event":"b4f3bdff83fbf2030f6e60c0ebf5f184946b84855482e2e02b43207f4ced1eb7","occurred_at":"2026-06-02T14:03:03.100000+00:00","prev":"c1c1ebd956c3928840f9f8a3e507c91bf9386cfa20e22aa1a5cacdb18edfc40f","seq":3}',
];

/** A mutable deep copy, so a tamper case can never bleed into another test's vector. */
function chain(): CustodyEvent[] {
  return structuredClone(CANONICAL_CHAIN) as CustodyEvent[];
}

/** Narrowing helper — keeps the failure assertions readable without non-null assertions. */
function at(events: CustodyEvent[], index: number): CustodyEvent {
  const event = events[index];
  if (event === undefined) {
    throw new Error(`test vector has no entry at index ${String(index)}`);
  }
  return event;
}

describe("custodyPreimage", () => {
  it("reproduces Python's json.dumps output byte for byte", () => {
    const events = chain();
    expect(custodyPreimage(at(events, 0))).toBe(CANONICAL_PREIMAGES[0]);
    expect(custodyPreimage(at(events, 1))).toBe(CANONICAL_PREIMAGES[1]);
    expect(custodyPreimage(at(events, 2))).toBe(CANONICAL_PREIMAGES[2]);
  });

  it("emits keys in Python's sort_keys order", () => {
    // Parsed back out rather than eyeballed, so the assertion is about the emitted bytes.
    const keys = Object.keys(
      JSON.parse(custodyPreimage(at(chain(), 0))) as Record<string, unknown>,
    );
    expect(keys).toEqual([
      "event_type",
      "evidence_id",
      "integrity_hash_at_event",
      "occurred_at",
      "prev",
      "seq",
    ]);
  });

  it("abbreviates prev_event_hash and sequence_number to prev and seq", () => {
    // The payload field names and the hashed key names differ, and only here. Asserted
    // explicitly because using the payload names is the single most likely way to break this.
    const preimage = custodyPreimage(at(chain(), 0));
    expect(preimage).toContain('"prev":');
    expect(preimage).toContain('"seq":');
    expect(preimage).not.toContain("prev_event_hash");
    expect(preimage).not.toContain("sequence_number");
  });

  it("contains no whitespace, matching separators=(',', ':')", () => {
    expect(custodyPreimage(at(chain(), 0))).not.toMatch(/\s/);
  });
});

describe("toPythonIsoformat", () => {
  it("rewrites the wire Z suffix to Python's +00:00 offset", () => {
    expect(toPythonIsoformat("2026-06-02T14:03:02Z")).toBe("2026-06-02T14:03:02+00:00");
  });

  it("preserves microsecond precision, including trailing zeros", () => {
    // A Date round-trip would truncate to milliseconds and drop the trailing zeros — the exact
    // bug this function exists to avoid.
    expect(toPythonIsoformat("2026-06-02T14:03:01.123456Z")).toBe(
      "2026-06-02T14:03:01.123456+00:00",
    );
    expect(toPythonIsoformat("2026-06-02T14:03:03.100000Z")).toBe(
      "2026-06-02T14:03:03.100000+00:00",
    );
  });

  it("omits a fractional part when the source has none", () => {
    expect(toPythonIsoformat("2026-06-02T14:03:02Z")).not.toContain(".");
  });

  it("passes through a value already in offset form", () => {
    expect(toPythonIsoformat("2026-06-02T14:03:02+00:00")).toBe("2026-06-02T14:03:02+00:00");
  });
});

describe("verifyCustodyChain", () => {
  it("verifies the canonical chain produced by the Python backend", async () => {
    await expect(verifyCustodyChain(chain())).resolves.toEqual({
      status: "verified",
      count: 3,
    });
  });

  it("verifies regardless of the order the server returned entries in", async () => {
    // Ordering is a display concern; verification sorts a copy by sequence_number.
    const shuffled = [at(chain(), 2), at(chain(), 0), at(chain(), 1)];
    await expect(verifyCustodyChain(shuffled)).resolves.toEqual({ status: "verified", count: 3 });
  });

  it("treats an empty ledger as nothing to verify, not as a failure", async () => {
    await expect(verifyCustodyChain([])).resolves.toEqual({ status: "idle" });
  });

  it("detects an altered event_type", async () => {
    const events = chain();
    at(events, 1).event_type = "exported";

    const result = await verifyCustodyChain(events);

    expect(result.status).toBe("failed");
    if (result.status === "failed") {
      expect(result.sequenceNumber).toBe(2);
      expect(result.reason).toContain("altered");
    }
  });

  it("detects an altered timestamp", async () => {
    const events = chain();
    at(events, 0).occurred_at = "2026-06-02T14:03:01.123457Z";

    const result = await verifyCustodyChain(events);

    expect(result.status).toBe("failed");
    if (result.status === "failed") {
      expect(result.sequenceNumber).toBe(1);
    }
  });

  it("detects a removed genesis entry via the sequence gap", async () => {
    // The remaining entries still chain to each other correctly, so only the sequence check
    // catches truncation from the front.
    const result = await verifyCustodyChain(chain().slice(1));

    expect(result.status).toBe("failed");
    if (result.status === "failed") {
      expect(result.sequenceNumber).toBe(2);
      expect(result.reason).toContain("missing");
    }
  });

  it("detects a broken prev_event_hash link", async () => {
    const events = chain();
    at(events, 2).prev_event_hash = "f".repeat(64);

    const result = await verifyCustodyChain(events);

    expect(result.status).toBe("failed");
    if (result.status === "failed") {
      expect(result.sequenceNumber).toBe(3);
      expect(result.reason).toContain("chain is broken");
    }
  });

  it("rejects a first entry that does not carry the genesis sentinel", async () => {
    const events = chain();
    at(events, 0).prev_event_hash = "a".repeat(64);

    const result = await verifyCustodyChain(events);

    expect(result.status).toBe("failed");
    if (result.status === "failed") {
      expect(result.sequenceNumber).toBe(1);
      expect(result.reason).toContain("genesis");
    }
  });

  it("exposes the genesis sentinel the backend actually stores", () => {
    expect(GENESIS_PREV_HASH).toBe("0".repeat(64));
    expect(at(chain(), 0).prev_event_hash).toBe(GENESIS_PREV_HASH);
  });
});
