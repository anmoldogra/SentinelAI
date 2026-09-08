"""Ledger entries re-verify after a real PostgreSQL round-trip — ADR-0003 §2, Wave 1.2.

The unit suite proves the preimage is complete and that tampering breaks the hash. It cannot
prove the thing that actually decides whether this subsystem works years from now: that a row
**written to Postgres and read back** still recomputes to the hash stored on it. That is exactly
what the Verification Engine (Wave 1.4) will do, and exactly what the pre-Wave-1.2 encoding got
wrong.

``platform.audit_log.details`` is the sharp edge. It is ``JSONB``, so Postgres parses the value,
discards key order, and returns it in its own order. Under the old ``json.dumps`` preimage a
re-read row could not reproduce its own hash — the ledger would have failed its own verification
with nothing having been tampered with. These tests assert **both** directions: that Postgres
really does reorder the stored object, and that the entry still verifies anyway.

Runs against a throwaway database that is created and dropped here. Skips cleanly when no
Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.modules.ingestion.models import Evidence, EvidenceCustodyEvent
from sentinelai.modules.ingestion.service import _custody_entry_hash
from sentinelai.platform.auth.audit import _compute_hash, record_audit_event
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.crypto.ledger import LEDGER_HASH_ALGO, LEDGER_PREIMAGE_VERSION
from sentinelai.platform.db.base import Base

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_NOW = datetime(2026, 9, 8, 12, 0, 0, 123456, tzinfo=UTC)

# Keys chosen so Postgres's jsonb ordering (length, then bytewise) cannot coincide with the
# insertion order below — otherwise the reordering assertion could pass vacuously.
_DETAILS = {
    "zulu": 1,
    "a": "Chaîne de contrôle",
    "mike": [1, 2, {"nested": True}],
    "bb": None,
    "alpha_long_key": 2.5,
}


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

    name = f"sentinelai_ledgertest_{uuid.uuid4().hex[:8]}"
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
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS ingestion"))
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[
                    AuditLog.__table__,
                    Evidence.__table__,
                    EvidenceCustodyEvent.__table__,
                ],
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


async def test_audit_entry_reverifies_after_a_jsonb_roundtrip(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The core Wave 1.2 guarantee, end to end through a real database."""
    actor = uuid.uuid4()
    target = uuid.uuid4()
    async with sessions() as session:
        await record_audit_event(
            session,
            actor_user_id=actor,
            actor_role="investigator",
            action="evidence.read",
            module="ingestion",
            target_type="evidence",
            target_id=target,
            ip_address="10.0.0.5",
            user_agent="console/1.0",
            details=dict(_DETAILS),
        )
        await session.commit()

    async with sessions() as session:
        row = (await session.execute(select(AuditLog))).scalar_one()

    # The stamped agility metadata is what a verifier dispatches on.
    assert row.hash_algo == LEDGER_HASH_ALGO
    assert row.preimage_version == LEDGER_PREIMAGE_VERSION

    recomputed = _compute_hash(
        prev_hash=row.prev_entry_hash,
        audit_id=row.audit_id,
        occurred_at=row.occurred_at,
        actor_user_id=row.actor_user_id,
        actor_role=row.actor_role,
        action=row.action,
        module=row.module,
        target_type=row.target_type,
        target_id=row.target_id,
        ip_address=row.ip_address,
        user_agent=row.user_agent,
        details=row.details,
    )
    assert recomputed == row.entry_hash


async def test_postgres_really_does_reorder_the_details_object(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Guards the guard: if jsonb preserved key order, the test above would prove nothing."""
    async with sessions() as session:
        await record_audit_event(
            session,
            actor_user_id=None,
            actor_role="system",
            action="probe",
            module="platform",
            details=dict(_DETAILS),
        )
        await session.commit()

    async with sessions() as session:
        row = (await session.execute(select(AuditLog))).scalar_one()

    assert row.details is not None
    assert list(row.details) != list(_DETAILS), "jsonb no longer reorders — revisit this suite"
    assert row.details == _DETAILS  # same mapping, different order


async def test_tampering_with_a_stored_audit_row_is_detected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Rewrite the attribution in the database and the stored hash no longer matches.

    Before Wave 1.2 this exact edit — changing who performed an action, and from where — left
    every hash in the chain valid, because neither field was in the preimage. This is the
    regression the increment exists to close, asserted against a real row rather than a fixture.
    """
    async with sessions() as session:
        await record_audit_event(
            session,
            actor_user_id=uuid.uuid4(),
            actor_role="investigator",
            action="evidence.export",
            module="ingestion",
            ip_address="10.0.0.5",
            details={"reason": "case review"},
        )
        await session.commit()

    async with sessions() as session:
        row = (await session.execute(select(AuditLog))).scalar_one()
        stored_hash = row.entry_hash
        # A privileged writer editing the table directly, exactly the ADR-0003 threat model.
        await session.execute(
            text(
                "UPDATE platform.audit_log SET actor_role = 'admin', ip_address = '10.0.0.99' "
                "WHERE audit_id = :id"
            ),
            {"id": row.audit_id},
        )
        await session.commit()

    async with sessions() as session:
        forged = (await session.execute(select(AuditLog))).scalar_one()

    recomputed = _compute_hash(
        prev_hash=forged.prev_entry_hash,
        audit_id=forged.audit_id,
        occurred_at=forged.occurred_at,
        actor_user_id=forged.actor_user_id,
        actor_role=forged.actor_role,
        action=forged.action,
        module=forged.module,
        target_type=forged.target_type,
        target_id=forged.target_id,
        ip_address=forged.ip_address,
        user_agent=forged.user_agent,
        details=forged.details,
    )
    assert forged.entry_hash == stored_hash  # the attacker left the hash column alone
    assert recomputed != stored_hash  # ...and the recomputation exposes them


async def test_audit_chain_links_across_multiple_entries(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Three real rows: each entry names its predecessor, and all three re-verify."""
    async with sessions() as session:
        for index in range(3):
            await record_audit_event(
                session,
                actor_user_id=uuid.uuid4(),
                actor_role="investigator",
                action=f"action.{index}",
                module="ingestion",
                details={"i": index},
            )
            await session.commit()

    async with sessions() as session:
        rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
        )

    assert len(rows) == 3
    expected_prev = "0" * 64
    for row in rows:
        assert row.prev_entry_hash == expected_prev
        recomputed = _compute_hash(
            prev_hash=row.prev_entry_hash,
            audit_id=row.audit_id,
            occurred_at=row.occurred_at,
            actor_user_id=row.actor_user_id,
            actor_role=row.actor_role,
            action=row.action,
            module=row.module,
            target_type=row.target_type,
            target_id=row.target_id,
            ip_address=row.ip_address,
            user_agent=row.user_agent,
            details=row.details,
        )
        assert recomputed == row.entry_hash
        expected_prev = row.entry_hash


async def test_custody_entry_reverifies_after_a_roundtrip(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The custody ledger's equivalent, including a non-ASCII note and a null actor.

    ``occurred_at`` is the field most likely to break here: Postgres returns a ``timestamptz`` with
    microsecond precision, and the preimage renders it back to the wire form. A truncation or an
    offset difference anywhere in that path shows up as a hash mismatch.
    """
    evidence_id = uuid.uuid4()
    custody_event_id = uuid.uuid4()
    async with sessions() as session:
        # The custody table carries a real FK to `evidence`, so the parent row must exist. Only
        # the NOT NULL columns are populated — this test is about the ledger, not about evidence.
        session.add(
            Evidence(
                evidence_id=evidence_id,
                schema_version="1.0.0",
                category="digital_forensics",
                artifact_type="file_artifact",
                title="round-trip probe",
                source={"connector": "test"},
                collected_at=_NOW,
                ingested_at=_NOW,
                attributes={},
                confidence=Decimal("1.0"),
                retention_policy_ref="default",
            )
        )
        await session.flush()
        session.add(
            EvidenceCustodyEvent(
                custody_event_id=custody_event_id,
                evidence_id=evidence_id,
                sequence_number=1,
                event_type="ingested",
                occurred_at=_NOW,
                actor_user_id=None,
                actor_role="system",
                authority_ref=None,
                notes="Chaîne de contrôle — vérifiée",
                integrity_hash_at_event="a" * 64,
                prev_event_hash="0" * 64,
                entry_hash=_custody_entry_hash(
                    prev_hash="0" * 64,
                    custody_event_id=custody_event_id,
                    evidence_id=evidence_id,
                    sequence_number=1,
                    event_type="ingested",
                    occurred_at=_NOW,
                    actor_user_id=None,
                    actor_role="system",
                    authority_ref=None,
                    notes="Chaîne de contrôle — vérifiée",
                    integrity_hash_at_event="a" * 64,
                ),
                hash_algo=LEDGER_HASH_ALGO,
                preimage_version=LEDGER_PREIMAGE_VERSION,
            )
        )
        await session.commit()

    async with sessions() as session:
        row = (await session.execute(select(EvidenceCustodyEvent))).scalar_one()

    assert row.preimage_version == LEDGER_PREIMAGE_VERSION
    assert row.occurred_at.microsecond == 123456, "microseconds must survive the round-trip"
    recomputed = _custody_entry_hash(
        prev_hash=row.prev_event_hash,
        custody_event_id=row.custody_event_id,
        evidence_id=row.evidence_id,
        sequence_number=row.sequence_number,
        event_type=row.event_type,
        occurred_at=row.occurred_at,
        actor_user_id=row.actor_user_id,
        actor_role=row.actor_role,
        authority_ref=row.authority_ref,
        notes=row.notes,
        integrity_hash_at_event=row.integrity_hash_at_event,
    )
    assert recomputed == row.entry_hash
