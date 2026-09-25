"""The Verification Engine against a live, genuinely tampered ledger — ADR-0003 §6, Wave 1.4.

``tests/unit/test_verification.py`` proves the engine's logic over constructed entries. This file
proves the thing that actually matters: that the engine, reading real rows back through the real
production read path, catches an attacker who altered a real database.

The difference is not academic. Every verdict here depends on
:func:`sentinelai.platform.auth.ledger_verification.audit_preimage_fields` rebuilding a persisted
row into byte-identical preimage bytes — through a JSONB round-trip, a ``TIMESTAMP WITH TIME ZONE``
round-trip, and asyncpg's type coercion. A unit test with hand-built dictionaries cannot detect a
drift in any of those, and a drift in any of them would make the entire subsystem report intact
history as forged.

The attacks are performed with real SQL. Several of them require dropping the ADR-0004 append-only
trigger first, which a superuser can do — and which is precisely why ADR-0003 says the database
alone
is never the guarantee.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.ledger_verification import (
    AuditLedgerVerificationService,
    read_anchor_views,
    read_audit_chain_hashes,
    read_recent_audit_entries,
    summarize_findings,
)
from sentinelai.platform.auth.models import AuditLog, LedgerAnchor
from sentinelai.platform.config import settings
from sentinelai.platform.crypto.anchoring import AnchorBatch, LedgerAnchorService
from sentinelai.platform.crypto.ledger import LEDGER_AUDIT, LedgerSigner
from sentinelai.platform.crypto.verification import (
    Finding,
    LedgerVerifier,
    VerificationState,
)
from sentinelai.platform.db.base import Base
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_BUCKET = "sentinelai-anchors"


async def _reachable(url: str) -> bool:
    try:
        engine = create_async_engine(url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A throwaway database with the two tables verification reads.

    A fresh database per test rather than a shared schema: these tests delete and rewrite ledger
    rows, and a leaked mutation would silently corrupt an unrelated test's verdict — the one kind of
    cross-test interference that would be almost impossible to diagnose from a failure message.
    """
    if not await _reachable(_URL):
        pytest.skip(f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL")

    name = f"sentinelai_verifytest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    engine: AsyncEngine = create_async_engine(_URL.rsplit("/", 1)[0] + f"/{name}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
            await conn.run_sync(
                Base.metadata.create_all, tables=[AuditLog.__table__, LedgerAnchor.__table__]
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


def _signer() -> LedgerSigner:
    return LedgerSigner(kms_for_tests())


async def _write_entries(sessions: async_sessionmaker[AsyncSession], count: int) -> None:
    """Append ``count`` real audit entries through the production write path.

    One transaction each, because that is how they are really written — and because the chain lock
    is
    held to commit, so batching them into one transaction would not exercise the same path.
    """
    for index in range(count):
        async with sessions() as session:
            await record_audit_event(
                session,
                kms=kms_for_tests(),
                actor_user_id=uuid.uuid4(),
                actor_role="investigator",
                action=f"action.{index}",
                module="ingestion",
                target_type="evidence",
                target_id=uuid.uuid4(),
                ip_address="10.0.0.5",
                user_agent="pytest",
                details={"i": index, "nested": {"k": ["v", 1, True, None]}},
            )
            await session.commit()


async def _verify(session: AsyncSession) -> object:
    return await AuditLedgerVerificationService(session, _signer()).verify()


async def _publish_anchor_over_everything(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Anchor the whole current ledger and record the row, as the batch job will."""
    storage = FakeObjectStorage()
    service = LedgerAnchorService(kms_for_tests(), storage, bucket=_BUCKET)
    async with sessions() as session:
        hashes = await read_audit_chain_hashes(session)
        anchor = await service.publish(AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=tuple(hashes)))
        session.add(
            LedgerAnchor(
                anchor_id=anchor.anchor_id,
                ledger=anchor.ledger,
                merkle_root=anchor.merkle_root,
                merkle_hash_algo=anchor.merkle_hash_algo,
                first_entry_hash=anchor.first_entry_hash,
                last_entry_hash=anchor.last_entry_hash,
                entry_count=anchor.entry_count,
                created_at=anchor.created_at,
                signature=anchor.signature.envelope,
                sig_alg=anchor.signature.sig_alg,
                key_id=anchor.signature.key_id,
                worm_object_ref=anchor.worm_object_ref,
            )
        )
        await session.commit()


async def _drop_append_only_trigger(session: AsyncSession) -> None:
    """Remove ADR-0004's backstop so the attacks below can actually run.

    A superuser can do this, which is exactly ADR-0003's argument for anchoring: the database's own
    protections are administered by the person the evidence most needs protecting from.
    """
    await session.execute(
        text("DROP TRIGGER IF EXISTS audit_log_append_only ON platform.audit_log")
    )
    await session.commit()


# ---------------------------------------------------------------------------------------
# The baseline. Without this passing, every negative result below could be a false positive
# caused by a preimage that simply never round-trips.
# ---------------------------------------------------------------------------------------


async def test_a_real_untouched_ledger_verifies_end_to_end(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Rows written by the real writer and read back by the real reader must verify.

    This is the test that proves the preimage survives persistence: JSONB for ``details``,
    ``TIMESTAMPTZ`` for ``occurred_at``, ``uuid`` for the actor and target. If any of those came
    back
    in a form that canonicalized differently, every entry here would report HASH_MISMATCH.
    """
    await _write_entries(sessions, 6)

    async with sessions() as session:
        report = await _verify(session)

    assert report.state is VerificationState.VERIFIED
    assert report.entry_count == 6
    assert report.verified_entries == 6
    assert report.failed_entries == 0
    assert report.findings == ()


async def test_the_genesis_entry_is_verifiable(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The first entry's ``prev_entry_hash`` is the all-zero sentinel, stored and hashed
    literally."""
    await _write_entries(sessions, 1)

    async with sessions() as session:
        entries = await read_recent_audit_entries(session)
        report = await LedgerVerifier(_signer()).verify_chain(
            ledger=LEDGER_AUDIT, entries=entries, expect_genesis=True
        )

    assert report.state is VerificationState.VERIFIED
    assert entries[0].prev_hash == "0" * 64


# ---------------------------------------------------------------------------------------
# Tamper injection — real UPDATEs against a real ledger
# ---------------------------------------------------------------------------------------


async def test_rewriting_an_actor_role_in_the_database_is_detected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The privilege-escalation forgery: change who acted, after the fact.

    ADR-0003 Context §2 names these attribution fields as the ones the pre-Wave-1.2 preimage left
    forgeable. This proves they are now both covered *and checked*.
    """
    await _write_entries(sessions, 4)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        target = (
            await session.execute(select(AuditLog).order_by(AuditLog.occurred_at).limit(1))
        ).scalar_one()
        await session.execute(
            update(AuditLog).where(AuditLog.audit_id == target.audit_id).values(actor_role="admin")
        )
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    assert report.state is VerificationState.FAILED
    assert Finding.HASH_MISMATCH in report.entries[0].findings
    # The signature still covers the (unchanged) entry_hash, so it remains valid - the hash
    # recompute
    # is the layer that catches this one. Worth asserting: it shows the layers are independent.
    assert Finding.SIGNATURE_INVALID not in report.entries[0].findings


async def test_rewriting_the_details_payload_is_detected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """JSONB is where a naive preimage leaks: key order and whitespace are not preserved by
    Postgres.

    JCS is what makes this detectable rather than flaky - the canonical form is stable across the
    round trip, so a *real* change shows up and a re-serialization does not.
    """
    await _write_entries(sessions, 3)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        target = (
            await session.execute(select(AuditLog).order_by(AuditLog.occurred_at).limit(1))
        ).scalar_one()
        await session.execute(
            update(AuditLog)
            .where(AuditLog.audit_id == target.audit_id)
            .values(details={"i": 999, "nested": {"k": ["v", 1, True, None]}})
        )
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    assert report.state is VerificationState.FAILED
    assert Finding.HASH_MISMATCH in report.entries[0].findings


async def test_a_reserialized_but_unchanged_jsonb_payload_still_verifies(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The false-positive control, and the reason JCS was chosen over ``json.dumps``.

    Rewriting ``details`` with the same content but different key order must NOT be reported as
    tampering. Without canonical encoding this test fails, and the whole subsystem would cry wolf
    every time Postgres chose to store an equivalent JSONB differently.
    """
    await _write_entries(sessions, 2)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        target = (
            await session.execute(select(AuditLog).order_by(AuditLog.occurred_at).limit(1))
        ).scalar_one()
        original = dict(target.details or {})
        reordered = {"nested": original["nested"], "i": original["i"]}
        await session.execute(
            update(AuditLog).where(AuditLog.audit_id == target.audit_id).values(details=reordered)
        )
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    assert report.state is VerificationState.VERIFIED


async def test_replacing_a_signature_with_another_entrys_is_detected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A signature is bound to its own entry, so it cannot be transplanted."""
    await _write_entries(sessions, 3)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )
        await session.execute(
            update(AuditLog)
            .where(AuditLog.audit_id == rows[2].audit_id)
            .values(signature=rows[0].signature)
        )
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    assert report.state is VerificationState.FAILED
    assert Finding.SIGNATURE_INVALID in report.entries[2].findings


async def test_a_recomputed_chain_is_still_caught_by_the_signatures(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The attack that defeats hashing entirely, run for real.

    An attacker edits a row and recomputes every downstream ``entry_hash`` so the chain is
    internally
    perfect. Nothing about a hash requires a secret, so this succeeds against link continuity and
    hash recomputation alike. Only the KMS signature stops it, and this is the proof.
    """
    await _write_entries(sessions, 3)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )
        # Forge row 0's attribution, then rebuild every hash and link downstream of it exactly as a
        # competent attacker with write access would.
        from sentinelai.platform.auth.ledger_verification import audit_entry_view
        from sentinelai.platform.crypto.ledger import compute_entry_hash

        prev = rows[0].prev_entry_hash
        for position, row in enumerate(rows):
            if position == 0:
                row.actor_role = "admin"
            row.prev_entry_hash = prev
            view = audit_entry_view(row)
            assert view.preimage_fields is not None
            new_hash = compute_entry_hash(view.preimage_fields)
            await session.execute(
                update(AuditLog)
                .where(AuditLog.audit_id == row.audit_id)
                .values(actor_role=row.actor_role, prev_entry_hash=prev, entry_hash=new_hash)
            )
            prev = new_hash
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    # The chain is structurally flawless - that is what makes this attack worth defending against.
    assert Finding.LINK_BROKEN not in report.findings
    assert Finding.HASH_MISMATCH not in report.findings
    # And every signature now covers the wrong entry_hash.
    assert report.state is VerificationState.FAILED
    assert Finding.SIGNATURE_INVALID in report.entries[0].findings


# ---------------------------------------------------------------------------------------
# Truncation — the attack only the anchor layer can see
# ---------------------------------------------------------------------------------------


async def test_a_deleted_tail_is_invisible_without_anchors_and_caught_with_them(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Asserted in two halves, because the second is only meaningful given the first.

    A truncated ledger does not look broken. It looks *perfect* — shorter, and internally flawless.
    The first assertion establishes that, so the second is unmistakably the anchor's doing.
    """
    await _write_entries(sessions, 6)
    await _publish_anchor_over_everything(sessions)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )
        for row in rows[4:]:
            await session.execute(
                text("DELETE FROM platform.audit_log WHERE audit_id = :i"), {"i": row.audit_id}
            )
        await session.commit()

    async with sessions() as session:
        entries = await read_recent_audit_entries(session)
        # Half one: the surviving chain, checked without anchors, is beyond reproach.
        chain_only = await LedgerVerifier(_signer()).verify_chain(
            ledger=LEDGER_AUDIT, entries=entries, expect_genesis=True
        )
        assert chain_only.state is VerificationState.VERIFIED
        assert chain_only.entry_count == 4

        # Half two: with the anchor, the missing rows are exposed.
        full = await _verify(session)

    assert full.state is VerificationState.FAILED
    assert full.anchors, "the anchor row must have been read back"
    assert Finding.ANCHOR_RANGE_MISSING in full.anchors[0].findings


async def test_deleting_an_interior_entry_is_caught_by_the_anchor_root(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Both ends of the anchored range survive, so only the recomputed root exposes this."""
    await _write_entries(sessions, 6)
    await _publish_anchor_over_everything(sessions)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )
        await session.execute(
            text("DELETE FROM platform.audit_log WHERE audit_id = :i"), {"i": rows[2].audit_id}
        )
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    assert report.state is VerificationState.FAILED
    findings = report.anchors[0].findings
    assert Finding.ANCHOR_ENTRY_COUNT_MISMATCH in findings
    assert Finding.ANCHOR_ROOT_MISMATCH in findings
    # The chain link is broken too - both layers independently notice, which is the design.
    assert Finding.LINK_BROKEN in report.findings


async def test_entries_appended_after_an_anchor_do_not_falsify_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Normal operation must not alarm: an anchor commits to a range, not to the ledger's end."""
    await _write_entries(sessions, 4)
    await _publish_anchor_over_everything(sessions)
    await _write_entries(sessions, 3)

    async with sessions() as session:
        report = await _verify(session)

    assert report.state is VerificationState.VERIFIED
    assert report.anchors[0].state is VerificationState.VERIFIED
    assert report.unanchored_entries == 3


async def test_a_forged_anchor_row_is_caught_by_its_signature(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """An attacker who truncates the ledger *and* re-anchors it must still fail.

    They can recompute a root over what remains, but they cannot sign it — the key is in a KMS the
    database role cannot read. So the anchor's own signature is verified unconditionally.
    """
    await _write_entries(sessions, 5)
    await _publish_anchor_over_everything(sessions)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )
        for row in rows[3:]:
            await session.execute(
                text("DELETE FROM platform.audit_log WHERE audit_id = :i"), {"i": row.audit_id}
            )
        # Re-anchor over the survivors, with an unsignable (forged) envelope.
        from sentinelai.platform.crypto.merkle import merkle_root

        survivors = [r.entry_hash for r in rows[:3]]
        await session.execute(text("DELETE FROM platform.ledger_anchors"))
        session.add(
            LedgerAnchor(
                anchor_id=uuid.uuid4(),
                ledger=LEDGER_AUDIT,
                merkle_root=merkle_root(survivors),
                merkle_hash_algo="SHA-256",
                first_entry_hash=survivors[0],
                last_entry_hash=survivors[-1],
                entry_count=len(survivors),
                created_at=datetime.now(UTC),
                signature=b"forged-envelope",
                sig_alg="Ed25519",
                key_id="dev:1:forged",
                worm_object_ref="anchors/forged.json",
            )
        )
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    # The range and root reconcile perfectly against the doctored ledger...
    assert Finding.ANCHOR_ROOT_MISMATCH not in report.anchors[0].findings
    # ...and the forgery is still caught.
    assert report.state is VerificationState.FAILED
    assert Finding.ANCHOR_SIGNATURE_INVALID in report.anchors[0].findings


# ---------------------------------------------------------------------------------------
# Legacy rows — the `partial` state against real data
# ---------------------------------------------------------------------------------------


async def test_a_pre_wave_1_2_row_is_reported_partial_not_failed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Rows with NULL signature/preimage_version exist in any database that predates Wave 1.2.

    They must be reported as *not independently verifiable*. Calling them failed would raise a
    tampering alarm over honest history on every single run, which is how a real alarm gets muted.
    """
    await _write_entries(sessions, 3)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        target = (
            await session.execute(select(AuditLog).order_by(AuditLog.occurred_at).limit(1))
        ).scalar_one()
        await session.execute(
            update(AuditLog)
            .where(AuditLog.audit_id == target.audit_id)
            .values(signature=None, preimage_version=None, hash_algo=None)
        )
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    assert report.state is VerificationState.PARTIAL
    assert report.partial_entries == 1
    assert report.failed_entries == 0
    assert Finding.UNSIGNED_LEGACY_ROW in report.entries[0].findings
    assert Finding.PREIMAGE_UNAVAILABLE in report.entries[0].findings
    # And critically, this must not trip the job's alarm.
    assert not report.is_failed


# ---------------------------------------------------------------------------------------
# The reader and the reporting helpers
# ---------------------------------------------------------------------------------------


async def test_the_entry_window_bounds_what_is_read(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The audit ledger is unbounded, so the entry sweep must be windowed and must take the tail."""
    await _write_entries(sessions, 7)

    async with sessions() as session:
        entries = await read_recent_audit_entries(session, limit=3)
        all_hashes = await read_audit_chain_hashes(session)

    assert len(entries) == 3
    assert len(all_hashes) == 7
    # Oldest-first within the window, and it is the *recent* end of the ledger.
    assert [e.entry_hash for e in entries] == all_hashes[-3:]


async def test_anchors_are_read_only_for_the_requested_ledger(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """One table serves both chains, so the discriminator has to actually discriminate.

    A custody anchor leaking into an audit report would be checked against the wrong chain and fail,
    manufacturing an alarm out of correct data.
    """
    await _write_entries(sessions, 2)
    await _publish_anchor_over_everything(sessions)

    async with sessions() as session:
        session.add(
            LedgerAnchor(
                anchor_id=uuid.uuid4(),
                ledger="ingestion.evidence_custody_events",
                merkle_root="c" * 64,
                merkle_hash_algo="SHA-256",
                first_entry_hash="d" * 64,
                last_entry_hash="e" * 64,
                entry_count=1,
                created_at=datetime.now(UTC),
                signature=b"other-ledger",
                sig_alg="Ed25519",
                key_id="dev:1:x",
                worm_object_ref="anchors/custody.json",
            )
        )
        await session.commit()

        audit_anchors = await read_anchor_views(session, LEDGER_AUDIT)
        custody_anchors = await read_anchor_views(session, "ingestion.evidence_custody_events")

    assert len(audit_anchors) == 1
    assert len(custody_anchors) == 1
    assert audit_anchors[0].ledger == LEDGER_AUDIT


async def test_findings_summary_counts_entry_and_anchor_findings_together(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """What the CRITICAL log line and the metric fan-out are built from."""
    await _write_entries(sessions, 4)
    await _publish_anchor_over_everything(sessions)

    async with sessions() as session:
        await _drop_append_only_trigger(session)
        rows = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )
        await session.execute(
            text("DELETE FROM platform.audit_log WHERE audit_id = :i"), {"i": rows[3].audit_id}
        )
        await session.commit()

    async with sessions() as session:
        report = await _verify(session)

    summary = summarize_findings(report)
    assert summary, "a failed report must summarize to at least one finding"
    assert str(Finding.ANCHOR_RANGE_MISSING) in summary
    assert all(isinstance(count, int) and count > 0 for count in summary.values())
    # Serializable, because it goes into a structured log line as-is.
    assert json.loads(json.dumps(summary)) == summary
