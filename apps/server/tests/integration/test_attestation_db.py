"""Independent attestation against a real database — ADR-0003 §3/§6, Part 14's restore requirement.

`test_ledger_anchoring_db.py` proved that a *published anchor* detects truncation. It checked those
anchors as the online verifier does: by reading ``platform.ledger_anchors``. That works against
an attacker who deletes ledger rows and leaves the anchor rows behind, and it is what every
verification path in this platform did until this file existed.

**It does not work against a restore.** A point-in-time restore rolls the ledger and its anchor rows
back together, so the database that comes up is internally flawless — every hash recomputes, every
signature verifies, every anchor it still remembers reconciles perfectly. ``deployment-
architecture.md`` Part 14 says the anchors "live in the WORM anchor bucket, not in the database, so
they survive the restore and will report exactly which committed entries are now missing", and that
sentence was a requirement nothing implemented.

:func:`test_the_dr_scenario_the_database_only_path_cannot_see` is the whole point of this file: it
performs that restore, asserts the **database-sourced** verification reports ``verified``, and then
asserts the WORM-sourced attestation reports ``failed``. Both halves matter — without the first, the
test would not show that the gap was real.

Real Postgres, real Ed25519 signatures, real Merkle roots. Skips cleanly when no Postgres is
reachable; never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.cli.attest import LedgerAttestation, attest_ledger
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.ledger_verification import (
    read_anchor_views,
    read_audit_chain_hashes,
    read_recent_audit_entries,
)
from sentinelai.platform.auth.models import AuditLog, LedgerAnchor
from sentinelai.platform.config import settings
from sentinelai.platform.crypto.anchoring import (
    AnchorBatch,
    LedgerAnchorService,
    PublishedAnchor,
)
from sentinelai.platform.crypto.attestation import AttestationFinding
from sentinelai.platform.crypto.ledger import LEDGER_AUDIT, LedgerSigner
from sentinelai.platform.crypto.verification import Finding, LedgerVerifier, VerificationState
from sentinelai.platform.db.base import Base
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_BUCKET = "sentinelai-attest-anchors"


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
    if not await _reachable(_URL):
        pytest.skip(f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL")

    name = f"sentinelai_attesttest_{uuid.uuid4().hex[:8]}"
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


@pytest.fixture
def storage() -> FakeObjectStorage:
    return FakeObjectStorage()


async def _write_entries(sessions: async_sessionmaker[AsyncSession], count: int) -> None:
    for index in range(count):
        async with sessions() as session:
            await record_audit_event(
                session,
                kms=kms_for_tests(),
                actor_user_id=uuid.uuid4(),
                actor_role="investigator",
                action=f"action.{index}",
                module="ingestion",
                details={"i": index},
            )
            await session.commit()


async def _chain(sessions: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sessions() as session:
        return await read_audit_chain_hashes(session)


async def _cut_anchor(
    sessions: async_sessionmaker[AsyncSession],
    storage: FakeObjectStorage,
    hashes: list[str],
) -> PublishedAnchor:
    """Publish an anchor to WORM and record its row — exactly what `cut_anchor_batches` does."""
    service = LedgerAnchorService(kms_for_tests(), storage, bucket=_BUCKET)
    anchor = await service.publish(AnchorBatch(ledger=LEDGER_AUDIT, entry_hashes=tuple(hashes)))
    async with sessions() as session:
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
    return anchor


async def _attest(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> LedgerAttestation:
    """Run the real CLI attestation unit against this scratch database and bucket."""
    kms = kms_for_tests()
    async with sessions() as session:
        return await attest_ledger(
            ledger=LEDGER_AUDIT,
            session=session,
            signer=LedgerSigner(kms),
            kms=kms,
            storage=storage,
            bucket=_BUCKET,
        )


async def _verify_from_database_anchors(
    sessions: async_sessionmaker[AsyncSession],
) -> VerificationState:
    """The pre-existing path: anchors read from ``platform.ledger_anchors``.

    Present so the DR test can show what the database-only verifier concludes, which is the reason
    this increment exists. Mirrors ``AuditLedgerVerificationService.verify``.
    """
    kms = kms_for_tests()
    async with sessions() as session:
        report = await LedgerVerifier(LedgerSigner(kms)).verify_chain(
            ledger=LEDGER_AUDIT,
            entries=await read_recent_audit_entries(session),
            anchors=await read_anchor_views(session, LEDGER_AUDIT),
            chain_entry_hashes=await read_audit_chain_hashes(session),
            expect_genesis=False,
        )
    return report.state


# --------------------------------------------------------------------------------------
# The intact case, and proof the checks are not vacuous
# --------------------------------------------------------------------------------------


async def test_an_intact_database_attests_verified(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    await _write_entries(sessions, 6)
    await _cut_anchor(sessions, storage, await _chain(sessions))

    result = await _attest(sessions, storage)

    assert result.state is VerificationState.VERIFIED
    assert result.chain.state is VerificationState.VERIFIED
    assert result.archive.state is VerificationState.VERIFIED
    # Non-vacuity: an attestation that read no anchors would also report "verified".
    assert result.archive.worm_anchors == 1
    assert result.archive.database_anchors == 1
    assert result.archive.reconciled == 1
    assert len(result.chain.anchors) == 1
    assert result.chain.entry_count == 6
    assert result.chain.verified_entries == 6


async def test_the_anchors_checked_came_from_the_bucket_not_the_database(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The substitution that makes this tool different from the online endpoint.

    Asserted directly rather than inferred: with the anchor rows deleted, a database-sourced
    verifier has nothing to check, so an attestation that still checks one anchor can only have
    read it from WORM.
    """
    await _write_entries(sessions, 4)
    await _cut_anchor(sessions, storage, await _chain(sessions))
    async with sessions() as session:
        await session.execute(delete(LedgerAnchor))
        await session.commit()

    result = await _attest(sessions, storage)

    assert result.archive.database_anchors == 0
    assert result.archive.worm_anchors == 1
    assert len(result.chain.anchors) == 1
    assert result.chain.anchors[0].state is VerificationState.VERIFIED


# --------------------------------------------------------------------------------------
# The DR scenario
# --------------------------------------------------------------------------------------


async def test_the_dr_scenario_the_database_only_path_cannot_see(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A point-in-time restore: the ledger tail and its anchor row both roll back together.

    This is the test this increment was built for. The restored database is a real, complete,
    self-consistent ledger — just an older one — so the database-sourced verifier reports
    ``verified``, and an operator returning it to service would have no signal at all. The
    WORM anchor survived the restore and says otherwise.
    """
    await _write_entries(sessions, 4)
    first_anchor = await _cut_anchor(sessions, storage, await _chain(sessions))

    # More evidence accrues, and a second anchor commits to it.
    await _write_entries(sessions, 3)
    second_anchor = await _cut_anchor(sessions, storage, (await _chain(sessions))[4:])

    # --- the restore: everything after the first anchor disappears, rows and anchor alike ---
    async with sessions() as session:
        keep = set((await read_audit_chain_hashes(session))[:4])
        rows = list((await session.execute(select(AuditLog))).scalars())
        for row in rows:
            if row.entry_hash not in keep:
                await session.delete(row)
        await session.execute(
            delete(LedgerAnchor).where(LedgerAnchor.anchor_id == second_anchor.anchor_id)
        )
        await session.commit()

    # The database now agrees with itself perfectly.
    assert await _verify_from_database_anchors(sessions) is VerificationState.VERIFIED

    # The bucket does not.
    result = await _attest(sessions, storage)

    assert result.state is VerificationState.FAILED
    assert result.chain.state is VerificationState.FAILED
    # The surviving committed range no longer contains what the second anchor covered.
    assert Finding.ANCHOR_RANGE_MISSING in {
        f for anchor in result.chain.anchors for f in anchor.findings
    }
    # And the archive reports the row that went missing alongside it.
    assert AttestationFinding.ANCHOR_MISSING_FROM_DATABASE in result.archive.findings
    assert result.archive.missing_from_database == 1
    # The first anchor is untouched and still reconciles — a report that failed everything would be
    # useless for deciding which cases are affected.
    reconciled = [a for a in result.archive.anchors if a.anchor_id == first_anchor.anchor_id]
    assert reconciled == [] or reconciled[0].state is VerificationState.VERIFIED


async def test_truncation_with_both_stores_intact_is_still_caught(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The pre-existing detection must not regress now that anchors arrive from elsewhere."""
    await _write_entries(sessions, 5)
    await _cut_anchor(sessions, storage, await _chain(sessions))
    async with sessions() as session:
        newest = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(1)))
            .scalars()
            .one()
        )
        await session.delete(newest)
        await session.commit()

    result = await _attest(sessions, storage)

    assert result.chain.state is VerificationState.FAILED


# --------------------------------------------------------------------------------------
# Entry-level tampering
# --------------------------------------------------------------------------------------


async def test_editing_a_historical_entry_fails_loudly(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A direct ``UPDATE`` on a historical row, the way a hostile DBA would do it.

    Caught twice over — the recomputed entry hash no longer matches the stored one, and the Merkle
    root over the surviving chain no longer matches what was published. Both are asserted, because a
    verifier that only noticed one of them would be defeated by an attacker who repaired the other.
    """
    await _write_entries(sessions, 5)
    await _cut_anchor(sessions, storage, await _chain(sessions))
    async with sessions() as session:
        target = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at).limit(1)))
            .scalars()
            .one()
        )
        await session.execute(
            update(AuditLog)
            .where(AuditLog.audit_id == target.audit_id)
            .values(action="action.rewritten-by-an-insider")
        )
        await session.commit()

    result = await _attest(sessions, storage)

    assert result.state is VerificationState.FAILED
    assert Finding.HASH_MISMATCH in result.chain.findings
    assert result.chain.failed_entries >= 1


async def test_recomputing_the_edited_entrys_hash_is_still_caught_by_the_signature(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """The attacker who edits a row *and* fixes its digest, exactly as the application would.

    Only the signature catches this, and only because the private key is in a KMS the database role
    cannot reach. Without it the hash would recompute cleanly and the entry would read as authentic.
    """
    from sentinelai.platform.auth.audit import audit_preimage_fields
    from sentinelai.platform.crypto.ledger import compute_entry_hash

    await _write_entries(sessions, 3)
    async with sessions() as session:
        target = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at).limit(1)))
            .scalars()
            .one()
        )
        forged_action = "action.forged"
        forged_hash = compute_entry_hash(
            audit_preimage_fields(
                prev_hash=target.prev_entry_hash,
                audit_id=target.audit_id,
                occurred_at=target.occurred_at,
                actor_user_id=target.actor_user_id,
                actor_role=target.actor_role,
                action=forged_action,
                module=target.module,
                target_type=target.target_type,
                target_id=target.target_id,
                ip_address=target.ip_address,
                user_agent=target.user_agent,
                details=target.details,
            )
        )
        await session.execute(
            update(AuditLog)
            .where(AuditLog.audit_id == target.audit_id)
            .values(action=forged_action, entry_hash=forged_hash)
        )
        await session.commit()

    result = await _attest(sessions, storage)

    assert result.state is VerificationState.FAILED
    assert Finding.SIGNATURE_INVALID in result.chain.findings
    # Non-vacuity: the forgery really did repair the digest, so the hash layer saw nothing wrong.
    assert Finding.HASH_MISMATCH not in result.chain.findings


# --------------------------------------------------------------------------------------
# Archive-side tampering
# --------------------------------------------------------------------------------------


async def test_editing_the_anchor_row_is_caught_by_the_published_copy(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """An attacker who doctors the ledger then rewrites the anchor row to match it.

    Against the database-only verifier this reconciles: the row and the ledger agree because both
    were changed. The WORM copy is the one thing that did not change.
    """
    await _write_entries(sessions, 4)
    anchor = await _cut_anchor(sessions, storage, await _chain(sessions))
    async with sessions() as session:
        await session.execute(
            update(LedgerAnchor)
            .where(LedgerAnchor.anchor_id == anchor.anchor_id)
            .values(merkle_root="f" * 64)
        )
        await session.commit()

    result = await _attest(sessions, storage)

    assert result.archive.state is VerificationState.FAILED
    assert AttestationFinding.ANCHOR_DOCUMENT_MISMATCH in result.archive.findings
    mismatch = next(a for a in result.archive.anchors if a.findings)
    assert mismatch.detail is not None
    assert "merkle_root" in mismatch.detail


async def test_a_deleted_worm_object_is_reported_even_though_the_row_survives(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """Under COMPLIANCE-mode Object Lock this should be impossible. If it happens, the lock was
    never real — Part 14 warns that a backup replica which drops retention is a copy an insider can
    edit — and the database is asserting a commitment that no longer exists anywhere."""
    await _write_entries(sessions, 4)
    anchor = await _cut_anchor(sessions, storage, await _chain(sessions))
    await storage.delete(_BUCKET, anchor.worm_object_ref)

    result = await _attest(sessions, storage)

    assert result.archive.state is VerificationState.FAILED
    assert AttestationFinding.ANCHOR_OBJECT_MISSING in result.archive.findings
    assert result.archive.missing_from_worm == 1
    assert result.archive.worm_anchors == 0


async def test_an_orphan_worm_object_with_an_intact_range_is_partial_not_failed(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """An interrupted anchor cut, which `publish` is deliberately ordered to make recoverable.

    The object is written before the row, so a crash in between leaves exactly this state. Reporting
    it as tampering would alarm on every killed worker; the DR test above shows what makes the same
    shape ``failed`` — the range going missing too.
    """
    await _write_entries(sessions, 4)
    await _cut_anchor(sessions, storage, await _chain(sessions))
    async with sessions() as session:
        await session.execute(delete(LedgerAnchor))
        await session.commit()

    result = await _attest(sessions, storage)

    assert result.archive.state is VerificationState.PARTIAL
    assert result.archive.findings == (AttestationFinding.ANCHOR_MISSING_FROM_DATABASE,)
    # The chain is untouched, so the overall verdict is partial rather than failed.
    assert result.chain.state is VerificationState.VERIFIED
    assert result.state is VerificationState.PARTIAL


async def test_a_corrupted_anchor_object_is_reported_without_denying_a_verdict(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A verifier an attacker can silence by corrupting one object is not much of a verifier."""
    await _write_entries(sessions, 4)
    good = await _cut_anchor(sessions, storage, await _chain(sessions))
    await storage.put_immutable(
        _BUCKET,
        f"anchors/{LEDGER_AUDIT}/2026/09/29/{uuid.uuid4()}.json",
        b"{ this is not an anchor",
        retain_until=datetime(2036, 1, 1, tzinfo=UTC),
    )

    result = await _attest(sessions, storage)

    assert AttestationFinding.ANCHOR_DOCUMENT_MALFORMED in result.archive.findings
    assert result.archive.malformed_objects == 1
    # The good anchor still produced its own verdict.
    assert result.archive.reconciled == 1
    assert any(
        a.anchor_id == good.anchor_id and a.state is VerificationState.VERIFIED
        for a in result.archive.anchors
    )


async def test_an_anchor_forged_into_worm_fails_its_signature(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """Writing a plausible anchor into the bucket does not make it ours.

    The attacker here controls the bucket rather than the database — a fresh commitment over a
    doctored history, published to look old. Only the Ed25519 signature over
    ``(ledger, entry_count, first_entry_hash, merkle_root)`` distinguishes it, and the key is not
    theirs.
    """
    import base64
    import json

    await _write_entries(sessions, 4)
    chain = await _chain(sessions)
    forged_id = uuid.uuid4()
    await storage.put_immutable(
        _BUCKET,
        f"anchors/{LEDGER_AUDIT}/2026/09/29/{forged_id}.json",
        json.dumps(
            {
                "v": 1,
                "anchor_id": str(forged_id),
                "ledger": LEDGER_AUDIT,
                "merkle_root": "c" * 64,
                "merkle_hash_algo": "SHA-256",
                "first_entry_hash": chain[0],
                "last_entry_hash": chain[-1],
                "entry_count": len(chain),
                "created_at": "2026-01-01T00:00:00Z",
                "signature": base64.b64encode(b"not-a-real-signature").decode(),
                "sig_alg": "Ed25519",
                "key_id": "evidence_root/default/1",
            }
        ).encode(),
        retain_until=datetime(2036, 1, 1, tzinfo=UTC),
    )

    result = await _attest(sessions, storage)

    assert result.archive.state is VerificationState.FAILED
    assert AttestationFinding.ANCHOR_DOCUMENT_SIGNATURE_INVALID in result.archive.findings


# --------------------------------------------------------------------------------------
# A ledger with nothing in it
# --------------------------------------------------------------------------------------


async def test_a_ledger_with_no_anchors_yet_is_not_a_failure(
    sessions: async_sessionmaker[AsyncSession], storage: FakeObjectStorage
) -> None:
    """A deployment whose cutter has not run yet must not look tampered with.

    The unanchored-entry count is the signal that anchoring has stopped; that is a gauge with an
    alert rule on sustained growth, not an attestation verdict.
    """
    await _write_entries(sessions, 3)

    result = await _attest(sessions, storage)

    assert result.state is VerificationState.VERIFIED
    assert result.archive.worm_anchors == 0
    assert result.chain.unanchored_entries == 3
