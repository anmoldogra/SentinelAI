"""The verification engine's three layers and three states — ADR-0003 §6, Wave 1.4.

No database: the engine is pure by design, so every one of its verdicts can be provoked here by
handing it constructed entries. The database-level attacks (a real ``UPDATE`` on a real ledger, a
deleted tail, a restore) live in ``tests/integration/test_verification_db.py``, because proving that
the engine catches tampering means tampering with something real.

Real Ed25519 from the dev KMS provider throughout. A stub signer would make every assertion in this
file vacuous: the entire claim is that a forged entry fails verification, and only real asymmetric
crypto can demonstrate that.

Tests are named for the property they defend, not the method they call, because the properties are
what must survive a refactor.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from sentinelai.platform.crypto.ledger import (
    LEDGER_AUDIT,
    LEDGER_CUSTODY,
    LEDGER_HASH_ALGO,
    LEDGER_PREIMAGE_VERSION,
    LedgerSigner,
    compute_entry_hash,
    ledger_timestamp,
)
from sentinelai.platform.crypto.merkle import merkle_root
from sentinelai.platform.crypto.verification import (
    GENESIS_HASH,
    AnchorView,
    Finding,
    LedgerEntryView,
    LedgerVerifier,
    VerificationState,
)
from tests.fixtures.kms import kms_for_tests

_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _signer() -> LedgerSigner:
    return LedgerSigner(kms_for_tests())


def _custody_fields(*, prev: str, seq: int, event_id: UUID, evidence_id: UUID) -> dict[str, object]:
    """A complete, canonical custody preimage — the same shape the writer builds."""
    return {
        "prev": prev,
        "custody_event_id": str(event_id),
        "evidence_id": str(evidence_id),
        "seq": seq,
        "event_type": "accessed",
        "occurred_at": ledger_timestamp(_NOW),
        "actor_user_id": str(UUID(int=7)),
        "actor_role": "investigator",
        "authority_ref": None,
        "notes": None,
        "integrity_hash_at_event": "a" * 64,
    }


async def _chain(length: int, *, evidence_id: UUID | None = None) -> list[LedgerEntryView]:
    """A genuinely valid signed custody chain of ``length`` entries, starting at genesis."""
    evidence_id = evidence_id or uuid4()
    signer = _signer()
    entries: list[LedgerEntryView] = []
    prev = GENESIS_HASH
    for seq in range(1, length + 1):
        fields = _custody_fields(
            prev=prev, seq=seq, event_id=UUID(int=seq), evidence_id=evidence_id
        )
        entry_hash = compute_entry_hash(fields)
        signature = await signer.sign(
            ledger=LEDGER_CUSTODY, sequence=seq, prev_hash=prev, entry_hash=entry_hash
        )
        entries.append(
            LedgerEntryView(
                sequence=seq,
                entry_hash=entry_hash,
                prev_hash=prev,
                hash_algo=LEDGER_HASH_ALGO,
                preimage_version=LEDGER_PREIMAGE_VERSION,
                preimage_fields=fields,
                signature_envelope=signature.envelope,
            )
        )
        prev = entry_hash
    return entries


async def _anchor_for(
    entries: list[LedgerEntryView], *, ledger: str = LEDGER_CUSTODY
) -> AnchorView:
    """A real, correctly-signed anchor committing to exactly ``entries``."""
    hashes = [e.entry_hash for e in entries]
    root = merkle_root(hashes)
    signature = await _signer().sign(
        ledger=f"anchor:{ledger}",
        sequence=len(hashes),
        prev_hash=hashes[0],
        entry_hash=root,
    )
    return AnchorView(
        anchor_id=uuid4(),
        ledger=ledger,
        merkle_root=root,
        first_entry_hash=hashes[0],
        last_entry_hash=hashes[-1],
        entry_count=len(hashes),
        signature_envelope=signature.envelope,
        worm_object_ref="anchors/test/anchor.json",
    )


# ---------------------------------------------------------------------------------------
# The baseline: an intact chain must verify. Every negative test below is only meaningful
# because this one passes.
# ---------------------------------------------------------------------------------------


async def test_an_intact_signed_chain_verifies_completely() -> None:
    entries = await _chain(4)
    report = await LedgerVerifier(_signer()).verify_chain(ledger=LEDGER_CUSTODY, entries=entries)

    assert report.state is VerificationState.VERIFIED
    assert (report.verified_entries, report.partial_entries, report.failed_entries) == (4, 0, 0)
    assert report.findings == ()
    assert not report.is_failed


async def test_an_empty_chain_reports_nothing_rather_than_failing() -> None:
    """An evidence item with no custody events has nothing to contradict.

    Reported as verified-with-zero-entries rather than failed: there is no evidence of tampering in
    the absence of a ledger, and a report that cried foul over emptiness would fire on every item
    between creation and its first custody event.
    """
    report = await LedgerVerifier(_signer()).verify_chain(ledger=LEDGER_CUSTODY, entries=[])

    assert report.state is VerificationState.VERIFIED
    assert report.entry_count == 0


# ---------------------------------------------------------------------------------------
# Layer 1 — link continuity
# ---------------------------------------------------------------------------------------


async def test_a_broken_link_is_detected_and_located() -> None:
    """The finding must name *which* entry broke, not just that the chain is broken."""
    entries = await _chain(4)
    tampered = [*entries[:2], replace(entries[2], prev_hash="f" * 64), *entries[3:]]

    report = await LedgerVerifier(_signer()).verify_chain(ledger=LEDGER_CUSTODY, entries=tampered)

    assert report.state is VerificationState.FAILED
    assert Finding.LINK_BROKEN in report.entries[2].findings
    assert report.entries[2].sequence == 3
    # The untouched entries must stay clean - a verifier that failed the whole chain would make the
    # report useless for locating the problem.
    assert report.entries[0].state is VerificationState.VERIFIED
    assert report.entries[1].state is VerificationState.VERIFIED


async def test_a_missing_genesis_is_distinguished_from_an_interior_break() -> None:
    """Losing the head of a chain reads very differently from a break in the middle."""
    entries = await _chain(3)
    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=entries[1:], expect_genesis=True
    )

    assert report.state is VerificationState.FAILED
    assert Finding.GENESIS_MISSING in report.entries[0].findings
    assert Finding.LINK_BROKEN not in report.entries[0].findings


async def test_a_window_into_a_global_ledger_does_not_demand_genesis() -> None:
    """The audit ledger is unbounded; a window over it legitimately starts mid-chain."""
    entries = await _chain(3)
    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=entries[1:], expect_genesis=False
    )

    assert report.state is VerificationState.VERIFIED
    assert Finding.GENESIS_MISSING not in report.findings


async def test_a_sequence_gap_is_detected() -> None:
    entries = await _chain(4)
    without_third = [entries[0], entries[1], entries[3]]
    # Re-link so the ONLY defect is the sequence jump - otherwise the link break would mask it and
    # the test would pass for the wrong reason.
    relinked = [
        *without_third[:2],
        replace(without_third[2], prev_hash=without_third[1].entry_hash),
    ]

    report = await LedgerVerifier(_signer()).verify_chain(ledger=LEDGER_CUSTODY, entries=relinked)

    assert Finding.SEQUENCE_GAP in report.entries[2].findings
    assert report.state is VerificationState.FAILED


async def test_a_duplicated_sequence_number_is_detected() -> None:
    entries = await _chain(3)
    duplicated = [entries[0], replace(entries[1], sequence=1)]

    report = await LedgerVerifier(_signer()).verify_chain(ledger=LEDGER_CUSTODY, entries=duplicated)

    assert Finding.SEQUENCE_DUPLICATE in report.entries[1].findings


# ---------------------------------------------------------------------------------------
# Layer 2 — entry authenticity
# ---------------------------------------------------------------------------------------


async def test_editing_an_attribution_field_is_detected_by_hash_recompute() -> None:
    """The forgeable fields ADR-0003 Context §2 names: who acted, in what role, under what
    authority.

    This is the attack Wave 1.2's complete preimage exists to stop, and the assertion that the
    preimage is actually *checked* rather than merely stored.
    """
    entries = await _chain(2)
    forged_fields = dict(entries[1].preimage_fields or {})
    forged_fields["actor_role"] = "admin"  # privilege escalation, after the fact
    tampered = replace(entries[1], preimage_fields=forged_fields)

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=[entries[0], tampered]
    )

    assert report.state is VerificationState.FAILED
    assert Finding.HASH_MISMATCH in report.entries[1].findings


async def test_a_recomputed_chain_still_fails_on_signatures() -> None:
    """The attack a hash alone cannot stop.

    An attacker with database access can edit a row *and* recompute every downstream ``entry_hash``,
    producing a chain that is internally perfect. Nothing about a hash requires a secret. Only the
    signature - made under a key the database role cannot read - survives that, and this test is the
    evidence that the engine checks it.
    """
    evidence_id = uuid4()
    signer = _signer()
    # Rebuild a fully self-consistent chain whose second entry carries forged attribution, hashing
    # and re-linking exactly as a competent attacker would.
    prev = GENESIS_HASH
    entries: list[LedgerEntryView] = []
    for seq in (1, 2):
        fields = _custody_fields(
            prev=prev, seq=seq, event_id=UUID(int=seq), evidence_id=evidence_id
        )
        if seq == 2:
            fields["actor_role"] = "admin"
        entry_hash = compute_entry_hash(fields)
        # Entry 1 is signed honestly; entry 2 is not signed at all by us, but carries entry 1's
        # envelope so the row *looks* signed to anything that only checks for presence.
        envelope = (
            await signer.sign(
                ledger=LEDGER_CUSTODY, sequence=seq, prev_hash=prev, entry_hash=entry_hash
            )
        ).envelope
        if seq == 2:
            envelope = entries[0].signature_envelope
        entries.append(
            LedgerEntryView(
                sequence=seq,
                entry_hash=entry_hash,
                prev_hash=prev,
                hash_algo=LEDGER_HASH_ALGO,
                preimage_version=LEDGER_PREIMAGE_VERSION,
                preimage_fields=fields,
                signature_envelope=envelope,
            )
        )
        prev = entry_hash

    report = await LedgerVerifier(signer).verify_chain(ledger=LEDGER_CUSTODY, entries=entries)

    # Links are intact and both hashes recompute correctly - the chain looks perfect.
    assert Finding.LINK_BROKEN not in report.findings
    assert Finding.HASH_MISMATCH not in report.findings
    # And it is still caught.
    assert report.state is VerificationState.FAILED
    assert Finding.SIGNATURE_INVALID in report.entries[1].findings


async def test_a_signature_from_the_other_ledger_does_not_verify() -> None:
    """Domain separation: an audit signature must not validate a custody entry.

    ``signed_message`` includes the ledger name for exactly this reason, and without the check a
    signature could be lifted between chains whenever the remaining fields coincided.
    """
    entries = await _chain(1)
    entry = entries[0]
    audit_signature = await _signer().sign(
        ledger=LEDGER_AUDIT,
        sequence=entry.sequence,
        prev_hash=entry.prev_hash,
        entry_hash=entry.entry_hash,
    )
    swapped = replace(entry, signature_envelope=audit_signature.envelope)

    report = await LedgerVerifier(_signer()).verify_chain(ledger=LEDGER_CUSTODY, entries=[swapped])

    assert Finding.SIGNATURE_INVALID in report.entries[0].findings


async def test_a_garbage_signature_envelope_fails_rather_than_raising() -> None:
    """A malformed envelope was presented as a signature and does not verify.

    It must not crash the report: a verifier that raised here would let one corrupt row deny a
    verdict on the entire chain.
    """
    entries = await _chain(1)
    broken = replace(entries[0], signature_envelope=b"not-an-envelope")

    report = await LedgerVerifier(_signer()).verify_chain(ledger=LEDGER_CUSTODY, entries=[broken])

    assert report.state is VerificationState.FAILED
    assert Finding.SIGNATURE_INVALID in report.entries[0].findings


# ---------------------------------------------------------------------------------------
# The three states - the distinction a courtroom depends on
# ---------------------------------------------------------------------------------------


async def test_a_legacy_unsigned_row_is_partial_not_failed() -> None:
    """Pre-Wave-1.2 history is *not provable*, which is not the same as *forged*.

    Reporting it as failed would be a false accusation of tampering against honest history; these
    rows cannot be signed retroactively because the bytes that should have been signed are gone.
    """
    entries = await _chain(2)
    legacy = replace(
        entries[0], signature_envelope=None, preimage_version=None, preimage_fields=None
    )

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=[legacy, entries[1]]
    )

    assert report.state is VerificationState.PARTIAL
    assert report.entries[0].state is VerificationState.PARTIAL
    assert Finding.UNSIGNED_LEGACY_ROW in report.entries[0].findings
    assert Finding.PREIMAGE_UNAVAILABLE in report.entries[0].findings
    assert report.partial_entries == 1
    assert report.failed_entries == 0
    # Critically: a partial chain must NOT raise the alarm the job listens for.
    assert not report.is_failed


async def test_one_failure_outranks_any_number_of_partials() -> None:
    """Severity must not be diluted: a chain containing a forgery is failed, full stop."""
    entries = await _chain(3)
    legacy = replace(
        entries[0], signature_envelope=None, preimage_version=None, preimage_fields=None
    )
    forged = replace(entries[2], signature_envelope=b"bad")

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=[legacy, entries[1], forged]
    )

    assert report.state is VerificationState.FAILED
    assert report.partial_entries == 1
    assert report.failed_entries == 1


async def test_an_unknown_preimage_version_is_partial_rather_than_a_false_accusation() -> None:
    """A row written by a future writer is out of this build's reach, not wrong.

    Guessing its field set would produce a mismatch and report a perfectly valid entry as forged.
    """
    entries = await _chain(1)
    future = replace(entries[0], preimage_version=99)

    report = await LedgerVerifier(_signer()).verify_chain(ledger=LEDGER_CUSTODY, entries=[future])

    assert report.state is VerificationState.PARTIAL
    assert Finding.UNKNOWN_PREIMAGE_VERSION in report.entries[0].findings


async def test_a_row_hashed_under_another_algorithm_is_partial() -> None:
    """Crypto agility: history stays verifiable under the algorithm it was written with.

    A SHA-384 row cannot be checked by a SHA-256 recompute, and saying so is honest; claiming a
    mismatch would not be.
    """
    entries = await _chain(1)
    other_algo = replace(entries[0], hash_algo="SHA-384")

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=[other_algo]
    )

    assert report.state is VerificationState.PARTIAL
    assert Finding.UNSUPPORTED_HASH_ALGO in report.entries[0].findings


# ---------------------------------------------------------------------------------------
# Layer 3 — anchor inclusion. The tail-truncation attack layers 1 and 2 cannot see.
# ---------------------------------------------------------------------------------------


async def test_an_intact_chain_verifies_against_its_anchor() -> None:
    entries = await _chain(4)
    anchor = await _anchor_for(entries)

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=entries, anchors=[anchor]
    )

    assert report.state is VerificationState.VERIFIED
    assert report.anchors[0].state is VerificationState.VERIFIED
    assert report.anchors[0].covered_entries == 4
    assert report.unanchored_entries == 0


async def test_a_truncated_tail_is_invisible_to_the_chain_but_caught_by_the_anchor() -> None:
    """The attack this entire layer exists for, asserted in two halves.

    First: the surviving chain is *internally perfect* - every link holds, every hash recomputes,
    every signature verifies. The point is not that a truncated ledger looks broken; it is that it
    looks flawless. Then: the anchor catches it anyway, because the commitment lives outside the
    database the attacker controls.
    """
    entries = await _chain(5)
    anchor = await _anchor_for(entries)
    survivors = entries[:3]  # the last two rows deleted

    # Half one: without the anchor, nothing is detectable.
    without_anchor = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=survivors
    )
    assert without_anchor.state is VerificationState.VERIFIED

    # Half two: with the anchor, it is.
    with_anchor = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=survivors, anchors=[anchor]
    )
    assert with_anchor.state is VerificationState.FAILED
    assert Finding.ANCHOR_RANGE_MISSING in with_anchor.anchors[0].findings


async def test_removing_an_interior_entry_is_caught_by_the_anchor_root() -> None:
    """Both ends of the range survive, so the root comparison is what catches this."""
    entries = await _chain(5)
    anchor = await _anchor_for(entries)
    gutted = [entries[0], entries[2], entries[3], entries[4]]

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=gutted, anchors=[anchor]
    )

    assert report.state is VerificationState.FAILED
    assert Finding.ANCHOR_ENTRY_COUNT_MISMATCH in report.anchors[0].findings
    assert Finding.ANCHOR_ROOT_MISMATCH in report.anchors[0].findings
    assert report.anchors[0].covered_entries == 4
    assert report.anchors[0].expected_entry_count == 5


async def test_reordering_entries_is_caught_because_order_is_committed_to() -> None:
    """A reordered custody chain is a different history, and the Merkle root says so."""
    entries = await _chain(4)
    anchor = await _anchor_for(entries)
    swapped = [entries[0], entries[2], entries[1], entries[3]]

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=swapped, anchors=[anchor]
    )

    assert Finding.ANCHOR_ROOT_MISMATCH in report.anchors[0].findings


async def test_a_forged_anchor_is_caught_by_its_own_signature() -> None:
    """An attacker who rewrote the ledger *and* the anchor row would reconcile perfectly.

    The range would match, the recomputed root would match the stored one, and only the anchor's
    signature - made under a key the database role cannot read - exposes it. So the anchor signature
    is checked unconditionally, never skipped because the root already agreed.
    """
    entries = await _chain(3)
    forged = AnchorView(
        anchor_id=uuid4(),
        ledger=LEDGER_CUSTODY,
        merkle_root=merkle_root([e.entry_hash for e in entries]),
        first_entry_hash=entries[0].entry_hash,
        last_entry_hash=entries[-1].entry_hash,
        entry_count=3,
        signature_envelope=b"forged",
        worm_object_ref="anchors/forged.json",
    )

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=entries, anchors=[forged]
    )

    assert Finding.ANCHOR_ROOT_MISMATCH not in report.anchors[0].findings
    assert Finding.ANCHOR_SIGNATURE_INVALID in report.anchors[0].findings
    assert report.state is VerificationState.FAILED


async def test_an_unanchored_tail_is_reported_but_is_not_a_failure() -> None:
    """Anchoring is batched, so recent entries are legitimately uncommitted.

    Counting that as a failure would make a healthy ledger alarm continuously; reporting the count
    is what lets an operator notice that batch cutting has stopped.
    """
    entries = await _chain(5)
    anchor = await _anchor_for(entries[:3])

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=entries, anchors=[anchor]
    )

    assert report.state is VerificationState.VERIFIED
    assert report.unanchored_entries == 2


async def test_an_anchor_outside_the_verified_window_is_checked_against_the_whole_chain() -> None:
    """Why ``chain_entry_hashes`` is separate from ``entries``.

    The audit ledger is verified entry-by-entry over a bounded window, but an anchor may commit to a
    range entirely outside it. Checked against the window alone, every older anchor would be
    reported as a missing range - a permanent false alarm. Given the full hash list, it verifies.
    """
    entries = await _chain(6)
    old_anchor = await _anchor_for(entries[:3])
    window = entries[3:]

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY,
        entries=window,
        anchors=[old_anchor],
        chain_entry_hashes=[e.entry_hash for e in entries],
        expect_genesis=False,
    )

    assert report.state is VerificationState.VERIFIED
    assert report.anchors[0].state is VerificationState.VERIFIED


async def test_the_window_alone_would_have_produced_a_false_alarm() -> None:
    """The control for the test above - proof that the separate scope is load-bearing."""
    entries = await _chain(6)
    old_anchor = await _anchor_for(entries[:3])

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY,
        entries=entries[3:],
        anchors=[old_anchor],
        expect_genesis=False,
    )

    assert report.state is VerificationState.FAILED
    assert Finding.ANCHOR_RANGE_MISSING in report.anchors[0].findings


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (VerificationState.VERIFIED, "verified"),
        (VerificationState.PARTIAL, "partial"),
        (VerificationState.FAILED, "failed"),
    ],
)
def test_states_serialize_to_their_wire_values(state: VerificationState, expected: str) -> None:
    """The API and the log lines both render these with ``str()``; the values are a contract."""
    assert str(state) == expected


async def test_a_preimage_that_cannot_be_canonicalized_fails_rather_than_passing() -> None:
    """An unrepresentable field set is a failure, not a legacy row.

    A row whose stored fields cannot be encoded as the bytes it was supposedly hashed from is not
    "old format" — something is wrong with it. Treating this as ``partial`` would let an attacker
    reach the tolerant branch by making a field unserializable.
    """
    entries = await _chain(1)
    # A set is not representable in JSON, so canonicalization raises.
    unencodable = replace(entries[0], preimage_fields={"prev": {1, 2, 3}})

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=[unencodable]
    )

    assert report.state is VerificationState.FAILED
    assert Finding.HASH_MISMATCH in report.entries[0].findings


async def test_an_anchor_whose_range_ends_before_it_starts_is_reported_missing() -> None:
    """A reversed range no longer exists as a range, which is what the anchor layer must report.

    Reachable when entries are reordered such that the committed last hash now precedes the first.
    """
    entries = await _chain(4)
    anchor = await _anchor_for(entries[1:3])
    # Swap the two anchored entries so `last` now sits before `first`.
    reordered = [entries[0], entries[2], entries[1], entries[3]]

    report = await LedgerVerifier(_signer()).verify_chain(
        ledger=LEDGER_CUSTODY, entries=reordered, anchors=[anchor]
    )

    assert report.state is VerificationState.FAILED
    assert Finding.ANCHOR_RANGE_MISSING in report.anchors[0].findings


def test_findings_summary_is_empty_for_a_clean_report() -> None:
    """The metric fan-out and the log line both iterate this; a clean run must yield nothing."""
    from sentinelai.platform.auth.ledger_verification import summarize_findings
    from sentinelai.platform.crypto.verification import LedgerVerificationReport

    clean = LedgerVerificationReport(
        ledger=LEDGER_CUSTODY,
        state=VerificationState.VERIFIED,
        entry_count=2,
        verified_entries=2,
        partial_entries=0,
        failed_entries=0,
    )
    assert summarize_findings(clean) == {}
    assert not clean.is_failed


def test_findings_summary_counts_repeats_across_entries_and_anchors() -> None:
    """Counts, not a set: three broken links is a materially different report from one."""
    from sentinelai.platform.auth.ledger_verification import summarize_findings
    from sentinelai.platform.crypto.verification import (
        AnchorFinding,
        EntryFinding,
        LedgerVerificationReport,
    )

    report = LedgerVerificationReport(
        ledger=LEDGER_AUDIT,
        state=VerificationState.FAILED,
        entry_count=3,
        verified_entries=0,
        partial_entries=1,
        failed_entries=2,
        entries=(
            EntryFinding(1, "a" * 64, VerificationState.FAILED, (Finding.LINK_BROKEN,)),
            EntryFinding(2, "b" * 64, VerificationState.FAILED, (Finding.LINK_BROKEN,)),
            EntryFinding(3, "c" * 64, VerificationState.PARTIAL, (Finding.UNSIGNED_LEGACY_ROW,)),
        ),
        anchors=(
            AnchorFinding(
                anchor_id=uuid4(),
                state=VerificationState.FAILED,
                expected_entry_count=3,
                covered_entries=2,
                worm_object_ref="anchors/x.json",
                findings=(Finding.ANCHOR_ROOT_MISMATCH,),
            ),
        ),
    )

    summary = summarize_findings(report)
    assert summary["link_broken"] == 2
    assert summary["unsigned_legacy_row"] == 1
    assert summary["anchor_root_mismatch"] == 1
    assert report.is_failed
