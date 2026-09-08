/**
 * Cross-language agreement between this verifier and the Python backend (ADR-0003, Wave 1.2).
 *
 * The fixtures below are **not hand-written**. Every `entry_hash` and every canonical preimage
 * string was produced by running the backend's own `_custody_entry_hash` and
 * `platform.crypto.canonical.canonicalize`, then pasted here verbatim. That is what makes this a
 * real cross-implementation test rather than a restatement of this file's own logic: if the two
 * canonicalizers ever disagree — on key order, on number rendering, on how a non-ASCII note or an
 * embedded quote is escaped — these hashes stop matching.
 *
 * The chain deliberately exercises the cases most likely to break agreement: entry 1 has null
 * `authority_ref`/`notes`; entry 2 carries accented text and an em dash; entry 3 has a null actor
 * and a note containing both a quote and a backslash. Timestamps cover whole seconds, half-second,
 * and full microsecond precision.
 *
 * Entries are named constants rather than array indices so no assertion is needed to read one.
 */

import { describe, expect, it } from "vitest";

import { canonicalJson } from "../../../shared/crypto/canonicalJson";
import type { CustodyEvent } from "../types";
import {
  custodyPreimage,
  GENESIS_PREV_HASH,
  SUPPORTED_PREIMAGE_VERSION,
  verifyCustodyChain,
} from "./verifyChain";

const EVIDENCE_ID = "1f3b2e2a-0000-4000-8000-000000000001";

const HASH_1 = "5a6d376b0f452124e643c1153d36779f513d4c272e5e9be702bf340ff72a6782";
const HASH_2 = "5e9e5eb6147a346bbd1541a1e4956ca09199920e3615c35791e62dec8cb71343";
const HASH_3 = "4b88d24f90d524ccf1367077e7313424732852a9fcee5951ae891ad3a7c1a30c";

const ENTRY_1: CustodyEvent = {
  custody_event_id: "aaaaaaaa-0000-4000-8000-000000000001",
  evidence_id: EVIDENCE_ID,
  sequence_number: 1,
  event_type: "ingested",
  occurred_at: "2026-09-08T10:00:00Z",
  actor_user_id: "33333333-3333-3333-3333-333333333333",
  actor_role: "investigator",
  authority_ref: null,
  integrity_hash_at_event: "a".repeat(64),
  prev_event_hash: GENESIS_PREV_HASH,
  entry_hash: HASH_1,
  notes: null,
  hash_algo: "SHA-256",
  preimage_version: 1,
};

const ENTRY_2: CustodyEvent = {
  custody_event_id: "aaaaaaaa-0000-4000-8000-000000000002",
  evidence_id: EVIDENCE_ID,
  sequence_number: 2,
  event_type: "accessed",
  occurred_at: "2026-09-08T11:30:00.500000Z",
  actor_user_id: "44444444-4444-4444-4444-444444444444",
  actor_role: "analyst",
  authority_ref: "warrant-2026-001",
  integrity_hash_at_event: "a".repeat(64),
  prev_event_hash: HASH_1,
  entry_hash: HASH_2,
  notes: "Chaîne de contrôle — vérifiée",
  hash_algo: "SHA-256",
  preimage_version: 1,
};

const ENTRY_3: CustodyEvent = {
  custody_event_id: "aaaaaaaa-0000-4000-8000-000000000003",
  evidence_id: EVIDENCE_ID,
  sequence_number: 3,
  event_type: "exported",
  occurred_at: "2026-09-08T12:00:00.123456Z",
  actor_user_id: null,
  actor_role: "system",
  authority_ref: null,
  integrity_hash_at_event: "b".repeat(64),
  prev_event_hash: HASH_2,
  entry_hash: HASH_3,
  notes: 'quote " and \\ backslash',
  hash_algo: "SHA-256",
  preimage_version: 1,
};

const CANONICAL_CHAIN: CustodyEvent[] = [ENTRY_1, ENTRY_2, ENTRY_3];

/** Each entry paired with the exact bytes Python's `canonicalize()` produced for it. */
const FIXTURES: readonly { event: CustodyEvent; preimage: string }[] = [
  {
    event: ENTRY_1,
    preimage:
      '{"actor_role":"investigator","actor_user_id":"33333333-3333-3333-3333-333333333333","authority_ref":null,"custody_event_id":"aaaaaaaa-0000-4000-8000-000000000001","event_type":"ingested","evidence_id":"1f3b2e2a-0000-4000-8000-000000000001","hash_algo":"SHA-256","integrity_hash_at_event":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","notes":null,"occurred_at":"2026-09-08T10:00:00Z","preimage_version":1,"prev":"0000000000000000000000000000000000000000000000000000000000000000","seq":1}',
  },
  {
    event: ENTRY_2,
    preimage:
      '{"actor_role":"analyst","actor_user_id":"44444444-4444-4444-4444-444444444444","authority_ref":"warrant-2026-001","custody_event_id":"aaaaaaaa-0000-4000-8000-000000000002","event_type":"accessed","evidence_id":"1f3b2e2a-0000-4000-8000-000000000001","hash_algo":"SHA-256","integrity_hash_at_event":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","notes":"Chaîne de contrôle — vérifiée","occurred_at":"2026-09-08T11:30:00.500000Z","preimage_version":1,"prev":"5a6d376b0f452124e643c1153d36779f513d4c272e5e9be702bf340ff72a6782","seq":2}',
  },
  {
    event: ENTRY_3,
    preimage:
      '{"actor_role":"system","actor_user_id":null,"authority_ref":null,"custody_event_id":"aaaaaaaa-0000-4000-8000-000000000003","event_type":"exported","evidence_id":"1f3b2e2a-0000-4000-8000-000000000001","hash_algo":"SHA-256","integrity_hash_at_event":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","notes":"quote \\" and \\\\ backslash","occurred_at":"2026-09-08T12:00:00.123456Z","preimage_version":1,"prev":"5e9e5eb6147a346bbd1541a1e4956ca09199920e3615c35791e62dec8cb71343","seq":3}',
  },
];

/** The chain with one entry patched — used to model a tampered or legacy row. */
function patched(target: CustodyEvent, patch: Partial<CustodyEvent>): CustodyEvent[] {
  return CANONICAL_CHAIN.map((event) =>
    event.sequence_number === target.sequence_number ? { ...event, ...patch } : { ...event },
  );
}

describe("canonicalJson agreement with the Python canonicalizer", () => {
  it("reproduces the backend's canonical preimage byte for byte", () => {
    for (const { event, preimage } of FIXTURES) {
      expect(canonicalJson(custodyPreimage(event))).toBe(preimage);
    }
  });

  it("sorts keys, so the field order is computed rather than transcribed", () => {
    const keys = [...canonicalJson(custodyPreimage(ENTRY_1)).matchAll(/"([a-z_]+)":/g)].map(
      (match) => match[1],
    );
    expect(keys).toEqual([...keys].sort());
  });

  it("emits non-ASCII literally rather than as escapes", () => {
    const encoded = canonicalJson(custodyPreimage(ENTRY_2));
    expect(encoded).toContain("Chaîne de contrôle — vérifiée");
    expect(encoded).not.toContain("\\u");
  });

  it("escapes quotes and backslashes exactly as Python does", () => {
    expect(canonicalJson(custodyPreimage(ENTRY_3))).toContain(
      '"notes":"quote \\" and \\\\ backslash"',
    );
  });

  it("renders integers without a decimal point", () => {
    expect(canonicalJson({ seq: 1, version: 1 })).toBe('{"seq":1,"version":1}');
  });

  it("contains no whitespace", () => {
    expect(canonicalJson(custodyPreimage(ENTRY_1))).not.toMatch(/\s/);
  });

  it("hashes the wire timestamp as received, with no rewriting", () => {
    // Wave 1.2 moved the backend to hashing the Z form, so what the client got is what was
    // hashed. The old +00:00 translation is gone and must not come back.
    expect(canonicalJson(custodyPreimage(ENTRY_1))).toContain(
      '"occurred_at":"2026-09-08T10:00:00Z"',
    );
  });
});

describe("verifyCustodyChain", () => {
  it("verifies the canonical chain produced by the Python backend", async () => {
    await expect(verifyCustodyChain([...CANONICAL_CHAIN])).resolves.toEqual({
      status: "verified",
      count: 3,
    });
  });

  it("verifies regardless of the order the server returned entries in", async () => {
    await expect(verifyCustodyChain([ENTRY_3, ENTRY_1, ENTRY_2])).resolves.toEqual({
      status: "verified",
      count: 3,
    });
  });

  it("treats an empty ledger as nothing to verify, not as a failure", async () => {
    await expect(verifyCustodyChain([])).resolves.toEqual({ status: "idle" });
  });

  // Each of these is a field that was NOT in the preimage before Wave 1.2. Every one of them
  // would have verified while altered.
  const attributionForgeries: [string, Partial<CustodyEvent>][] = [
    ["actor_role", { actor_role: "admin" }],
    ["actor_user_id", { actor_user_id: "99999999-9999-9999-9999-999999999999" }],
    ["authority_ref", { authority_ref: "warrant-2026-999" }],
    ["notes", { notes: "something else entirely" }],
    ["custody_event_id", { custody_event_id: "99999999-0000-4000-8000-000000000009" }],
  ];

  it.each(attributionForgeries)(
    "detects an altered %s — an attribution field Wave 1.2 added",
    async (_name, patch) => {
      await expect(verifyCustodyChain(patched(ENTRY_2, patch))).resolves.toMatchObject({
        status: "failed",
        sequenceNumber: 2,
      });
    },
  );

  it("detects an altered event_type", async () => {
    await expect(
      verifyCustodyChain(patched(ENTRY_1, { event_type: "disposed" })),
    ).resolves.toMatchObject({ status: "failed", sequenceNumber: 1 });
  });

  it("detects an altered timestamp", async () => {
    await expect(
      verifyCustodyChain(patched(ENTRY_3, { occurred_at: "2026-09-08T12:00:00.123457Z" })),
    ).resolves.toMatchObject({ status: "failed", sequenceNumber: 3 });
  });

  it("detects an altered integrity hash", async () => {
    await expect(
      verifyCustodyChain(patched(ENTRY_1, { integrity_hash_at_event: "c".repeat(64) })),
    ).resolves.toMatchObject({ status: "failed", sequenceNumber: 1 });
  });

  it("detects a removed genesis entry via the sequence gap", async () => {
    await expect(verifyCustodyChain([ENTRY_2, ENTRY_3])).resolves.toMatchObject({
      status: "failed",
      sequenceNumber: 2,
    });
  });

  it("detects a broken prev_event_hash link", async () => {
    await expect(
      verifyCustodyChain(patched(ENTRY_2, { prev_event_hash: "f".repeat(64) })),
    ).resolves.toMatchObject({ status: "failed", sequenceNumber: 2 });
  });

  it("rejects a first entry that does not carry the genesis sentinel", async () => {
    await expect(
      verifyCustodyChain([{ ...ENTRY_1, prev_event_hash: "9".repeat(64) }]),
    ).resolves.toMatchObject({ status: "failed", sequenceNumber: 1 });
  });

  it("exposes the genesis sentinel the backend actually stores", () => {
    expect(GENESIS_PREV_HASH).toBe("0".repeat(64));
    expect(ENTRY_1.prev_event_hash).toBe(GENESIS_PREV_HASH);
  });
});

describe("mixed-format chains", () => {
  /** An entry written before Wave 1.2: hashed over a partial field set, so not recomputable. */
  const legacyFirst = patched(ENTRY_1, { hash_algo: null, preimage_version: null });

  it("reports a legacy entry as unverifiable, never as altered", async () => {
    // The distinction is the whole point: calling an old-format entry "tampered" would be a
    // false accusation on a legal-custody surface.
    await expect(verifyCustodyChain(legacyFirst)).resolves.toEqual({
      status: "partial",
      verifiedCount: 2,
      unverifiableCount: 1,
    });
  });

  it("still checks linkage and sequence across a legacy entry", async () => {
    // The chain links do not depend on the preimage format, so they are verified either way — a
    // legacy entry is skipped for recomputation, not skipped entirely.
    const broken = legacyFirst.map((event) =>
      event.sequence_number === 2 ? { ...event, prev_event_hash: "e".repeat(64) } : event,
    );
    await expect(verifyCustodyChain(broken)).resolves.toMatchObject({
      status: "failed",
      sequenceNumber: 2,
    });
  });

  it("treats a downgraded preimage_version as unverifiable, not as verified", async () => {
    // The version is inside the hash, so claiming an older format cannot make a tampered entry
    // verify under weaker rules — the best an attacker achieves is "not checked".
    await expect(verifyCustodyChain(patched(ENTRY_2, { preimage_version: null }))).resolves.toEqual(
      { status: "partial", verifiedCount: 2, unverifiableCount: 1 },
    );
  });

  it("does not declare a future preimage version forged", async () => {
    // An old client meeting a newer entry must degrade, not accuse.
    await expect(
      verifyCustodyChain(patched(ENTRY_3, { preimage_version: SUPPORTED_PREIMAGE_VERSION + 1 })),
    ).resolves.toEqual({ status: "partial", verifiedCount: 2, unverifiableCount: 1 });
  });

  it("reports an all-legacy ledger as fully unverifiable", async () => {
    const allLegacy = CANONICAL_CHAIN.map((event) => ({
      ...event,
      hash_algo: null,
      preimage_version: null,
    }));
    await expect(verifyCustodyChain(allLegacy)).resolves.toEqual({
      status: "partial",
      verifiedCount: 0,
      unverifiableCount: 3,
    });
  });
});
