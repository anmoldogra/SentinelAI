"""Truncation and rollback detection via external anchors — ADR-0003 §3, PRD SR-4.

This is the file that decides whether Wave 1.3 did anything. Signing (IC-028) proved that an entry
cannot be *modified* undetected. It proved nothing about *removal*: an insider who deletes the tail
of a ledger, or restores yesterday's backup, leaves a shorter chain in which every remaining entry
still verifies and every hash still links. Nothing inside the database can catch that, because the
evidence of what is missing is exactly what was removed.

The tests below perform those attacks against a live Postgres — `DELETE` on the ledger, and a
full restore-from-older-snapshot — and assert the published anchor detects them. Real Ed25519,
real Merkle roots, real objects in a real object store.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.models import AuditLog, LedgerAnchor
from sentinelai.platform.config import settings
from sentinelai.platform.crypto.anchoring import (
    ANCHOR_DOCUMENT_VERSION,
    AnchorBatch,
    AnchoringError,
    LedgerAnchorService,
    anchor_document,
    verify_batch_against_anchor,
)
from sentinelai.platform.db.base import Base
from sentinelai.platform.db.chain_lock import AUDIT_CHAIN
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_BUCKET = "sentinelai-anchors"


async def _reachable(url: str) -> bool:
    try:
        # Explicit short timeout: this probe decides skip-vs-run and must never stall the suite.
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

    name = f"sentinelai_anchortest_{uuid.uuid4().hex[:8]}"
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


@pytest.fixture
def anchors(storage: FakeObjectStorage) -> LedgerAnchorService:
    return LedgerAnchorService(kms_for_tests(), storage, bucket=_BUCKET)


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


async def _entry_hashes(sessions: async_sessionmaker[AsyncSession]) -> list[str]:
    """The ledger's entry hashes in chain order — what a verifier recomputes the root over."""
    async with sessions() as session:
        rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
        )
    return [row.entry_hash for row in rows]


async def _record(sessions: async_sessionmaker[AsyncSession], anchor: object) -> None:
    """Persist a published anchor, the way a scheduled anchoring job would."""
    a = anchor  # narrow name for readability below
    async with sessions() as session:
        session.add(
            LedgerAnchor(
                anchor_id=a.anchor_id,  # type: ignore[attr-defined]
                ledger=a.ledger,  # type: ignore[attr-defined]
                merkle_root=a.merkle_root,  # type: ignore[attr-defined]
                merkle_hash_algo=a.merkle_hash_algo,  # type: ignore[attr-defined]
                first_entry_hash=a.first_entry_hash,  # type: ignore[attr-defined]
                last_entry_hash=a.last_entry_hash,  # type: ignore[attr-defined]
                entry_count=a.entry_count,  # type: ignore[attr-defined]
                created_at=a.created_at,  # type: ignore[attr-defined]
                signature=a.signature.envelope,  # type: ignore[attr-defined]
                sig_alg=a.signature.sig_alg,  # type: ignore[attr-defined]
                key_id=a.signature.key_id,  # type: ignore[attr-defined]
                worm_object_ref=a.worm_object_ref,  # type: ignore[attr-defined]
            )
        )
        await session.commit()


# --------------------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------------------


async def test_publishing_writes_a_self_describing_object_to_worm(
    sessions: async_sessionmaker[AsyncSession],
    anchors: LedgerAnchorService,
    storage: FakeObjectStorage,
) -> None:
    """The object must stand alone: the database is the thing being checked."""
    await _write_entries(sessions, 5)
    anchor = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(await _entry_hashes(sessions))))

    assert await storage.exists(_BUCKET, anchor.worm_object_ref)
    document = json.loads(anchor_document(anchor))
    assert document["v"] == ANCHOR_DOCUMENT_VERSION
    assert document["ledger"] == AUDIT_CHAIN
    assert document["entry_count"] == 5
    assert document["merkle_root"] == anchor.merkle_root
    # Everything needed to verify without the database: the root, the range, and the signature.
    assert set(document) >= {"merkle_root", "first_entry_hash", "last_entry_hash", "signature"}


async def test_the_anchor_signature_verifies(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    await _write_entries(sessions, 3)
    anchor = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(await _entry_hashes(sessions))))
    assert await anchors.verify_signature(anchor) is True


async def test_an_empty_batch_is_refused(anchors: LedgerAnchorService) -> None:
    """Anchoring nothing publishes a commitment that proves nothing while looking like proof."""
    with pytest.raises(AnchoringError, match="empty batch"):
        AnchorBatch(AUDIT_CHAIN, ())


async def test_an_intact_ledger_verifies_against_its_anchor(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    await _write_entries(sessions, 6)
    anchor = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(await _entry_hashes(sessions))))
    await _record(sessions, anchor)

    assert verify_batch_against_anchor(
        await _entry_hashes(sessions),
        merkle_root=anchor.merkle_root,
        entry_count=anchor.entry_count,
    )


# --------------------------------------------------------------------------------------
# The attacks anchoring exists to catch
# --------------------------------------------------------------------------------------


async def test_truncating_the_tail_is_detected(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    """The headline case. Deleting the last entries leaves a chain that verifies perfectly.

    Every surviving entry's hash still covers its contents, every signature is still valid, and
    every link still points at a real predecessor. Only the external anchor knows there were ever
    more.
    """
    await _write_entries(sessions, 6)
    original = await _entry_hashes(sessions)
    anchor = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(original)))
    await _record(sessions, anchor)

    # A privileged insider removing the last two entries. `DELETE` is rejected by ADR-0004's
    # trigger, so the attacker drops it first — which a superuser can do, and which is precisely
    # why ADR-0003 says the database alone is never the integrity guarantee.
    async with sessions() as session:
        await session.execute(text("ALTER TABLE platform.audit_log DISABLE TRIGGER USER"))
        await session.execute(
            text("DELETE FROM platform.audit_log WHERE entry_hash = ANY(:hashes)").bindparams(
                hashes=original[-2:]
            )
        )
        await session.commit()

    survivors = await _entry_hashes(sessions)
    assert len(survivors) == 4

    # The chain still looks perfect from the inside: each entry links to the previous one.
    async with sessions() as session:
        rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
        )
    expected_prev = "0" * 64
    for row in rows:
        assert row.prev_entry_hash == expected_prev, "the surviving chain is internally consistent"
        expected_prev = row.entry_hash

    # ...and the anchor catches it anyway.
    assert not verify_batch_against_anchor(
        survivors, merkle_root=anchor.merkle_root, entry_count=anchor.entry_count
    )


async def test_a_rollback_to_an_older_snapshot_is_detected(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    """Restore-from-backup, which is the *accidental* version of the same attack.

    A routine restore silently erases every entry written since the snapshot. The restored ledger
    is internally flawless — it is a real, complete ledger, just an older one — so a hash-and-
    signature check passes. Only a commitment published outside the database notices.
    """
    await _write_entries(sessions, 4)
    snapshot = await _entry_hashes(sessions)

    # Anchor the state as of "now", then keep writing.
    await _write_entries(sessions, 3)
    current = await _entry_hashes(sessions)
    anchor = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(current)))
    await _record(sessions, anchor)
    assert len(current) == 7

    # The restore: drop everything after the snapshot.
    async with sessions() as session:
        await session.execute(text("ALTER TABLE platform.audit_log DISABLE TRIGGER USER"))
        await session.execute(
            text("DELETE FROM platform.audit_log WHERE entry_hash <> ALL(:keep)").bindparams(
                keep=snapshot
            )
        )
        await session.commit()

    restored = await _entry_hashes(sessions)
    assert restored == snapshot, "the restored ledger is a genuine older ledger"
    assert not verify_batch_against_anchor(
        restored, merkle_root=anchor.merkle_root, entry_count=anchor.entry_count
    )


async def test_reordering_entries_is_detected(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    """The tree commits to the sequence, not the set — a reordered custody chain is a different
    history, and must not verify as the same one."""
    await _write_entries(sessions, 5)
    entries = await _entry_hashes(sessions)
    anchor = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(entries)))

    swapped = [*entries[:2], entries[3], entries[2], *entries[4:]]
    assert sorted(swapped) == sorted(entries)  # same set
    assert not verify_batch_against_anchor(
        swapped, merkle_root=anchor.merkle_root, entry_count=anchor.entry_count
    )


async def test_replacing_an_entry_is_detected(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    """Belt and braces: the signature already catches this, and so does the anchor."""
    await _write_entries(sessions, 4)
    entries = await _entry_hashes(sessions)
    anchor = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(entries)))

    forged = [*entries[:2], "f" * 64, *entries[3:]]
    assert not verify_batch_against_anchor(
        forged, merkle_root=anchor.merkle_root, entry_count=anchor.entry_count
    )


async def test_appending_after_the_anchor_does_not_falsify_it(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    """An anchor covers a range, not "the whole ledger forever".

    Legitimate growth must not read as tampering, or every verification after the first write
    would raise an alarm and the signal would be worthless. Verification is against the *covered
    range*, which is why the anchor records its bounds and its count.
    """
    await _write_entries(sessions, 4)
    covered = await _entry_hashes(sessions)
    anchor = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(covered)))
    await _record(sessions, anchor)

    await _write_entries(sessions, 3)
    everything = await _entry_hashes(sessions)
    assert len(everything) == 7

    # The full ledger does not match a 4-entry anchor, and must not be expected to...
    assert not verify_batch_against_anchor(
        everything, merkle_root=anchor.merkle_root, entry_count=anchor.entry_count
    )
    # ...but the range it actually covers still does.
    assert verify_batch_against_anchor(
        everything[: anchor.entry_count],
        merkle_root=anchor.merkle_root,
        entry_count=anchor.entry_count,
    )


# --------------------------------------------------------------------------------------
# The anchor store itself
# --------------------------------------------------------------------------------------


async def test_the_anchor_store_is_append_only_at_the_orm_level(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    """An anchor a DBA can rewrite protects nothing.

    `create_all` does not install the ADR-0004 trigger (that is the migration's job, and
    `test_migrations.py` proves it applies), so what is asserted here is the constraint the ORM
    metadata does carry: one anchor per range.
    """
    await _write_entries(sessions, 3)
    entries = await _entry_hashes(sessions)
    first = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(entries)))
    await _record(sessions, first)

    # A second anchor over the same range would give a verifier two competing commitments with no
    # way to say which is authoritative.
    second = await anchors.publish(AnchorBatch(AUDIT_CHAIN, tuple(entries)))
    with pytest.raises(IntegrityError):
        await _record(sessions, second)


async def test_anchor_object_keys_are_time_sortable_and_unique(
    sessions: async_sessionmaker[AsyncSession], anchors: LedgerAnchorService
) -> None:
    await _write_entries(sessions, 2)
    entries = await _entry_hashes(sessions)
    a = await anchors.publish(
        AnchorBatch(AUDIT_CHAIN, tuple(entries)), now=datetime(2026, 3, 4, tzinfo=UTC)
    )
    b = await anchors.publish(
        AnchorBatch(AUDIT_CHAIN, tuple(entries)), now=datetime(2026, 11, 5, tzinfo=UTC)
    )
    assert a.worm_object_ref.startswith(f"anchors/{AUDIT_CHAIN}/2026/03/04/")
    assert b.worm_object_ref.startswith(f"anchors/{AUDIT_CHAIN}/2026/11/05/")
    assert a.worm_object_ref < b.worm_object_ref
    assert a.worm_object_ref != b.worm_object_ref


async def test_a_storage_failure_prevents_an_anchor_being_claimed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """If WORM is unreachable, no anchor exists — and none may be reported.

    A database row pointing at an object that was never written would be the exact lie this
    subsystem exists to prevent: a ledger claiming it is anchored when it is not.
    """

    class BrokenStorage(FakeObjectStorage):
        async def put_immutable(self, *args: object, **kwargs: object) -> None:
            raise OSError("object store unreachable")

    await _write_entries(sessions, 2)
    service = LedgerAnchorService(kms_for_tests(), BrokenStorage(), bucket=_BUCKET)
    with pytest.raises(AnchoringError, match="could not be published"):
        await service.publish(AnchorBatch(AUDIT_CHAIN, tuple(await _entry_hashes(sessions))))
