"""Ledger signatures against a real database and real keys — ADR-0003 §1, PRD SR-4.

This is the file that decides whether the increment actually did anything. Wave 1.2 made the
entry hash cover every field, which catches an attacker who edits one row and leaves the hash
alone. It does nothing against the threat ADR-0003 is really about: a **privileged insider with
direct database access**, who can edit a row *and* recompute its hash, and every subsequent hash,
because nothing about a hash requires a secret.

The tests below carry out exactly that attack against a live Postgres — editing rows with raw
``UPDATE``, recomputing the hash chain forward the way the application itself would — and assert
that signature verification still fails, because the one thing the attacker does not have is the
private key. The crypto is real Ed25519 from the dev provider; a stub would make every one of
these assertions vacuous.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
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
from sentinelai.platform.crypto.ledger import (
    LEDGER_AUDIT,
    LEDGER_CUSTODY,
    LedgerSigner,
)
from sentinelai.platform.db.base import Base
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_NOW = datetime(2026, 9, 8, 12, 0, 0, 123456, tzinfo=UTC)


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

    name = f"sentinelai_sigtest_{uuid.uuid4().hex[:8]}"
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
                tables=[AuditLog.__table__, Evidence.__table__, EvidenceCustodyEvent.__table__],
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


async def _write_audit(sessions: async_sessionmaker[AsyncSession], **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "actor_user_id": uuid.uuid4(),
        "actor_role": "investigator",
        "action": "evidence.export",
        "module": "ingestion",
        "target_type": "evidence",
        "target_id": uuid.uuid4(),
        "ip_address": "10.0.0.5",
        "user_agent": "console/1.0",
        "details": {"reason": "case review"},
    }
    kwargs.update(overrides)
    async with sessions() as session:
        await record_audit_event(session, kms=kms_for_tests(), **kwargs)  # type: ignore[arg-type]
        await session.commit()


async def _verify_audit(row: AuditLog) -> bool:
    return await _signer().verify(
        ledger=LEDGER_AUDIT,
        sequence=None,
        prev_hash=row.prev_entry_hash,
        entry_hash=row.entry_hash,
        envelope=row.signature,
    )


async def _fetch_audit(sessions: async_sessionmaker[AsyncSession]) -> AuditLog:
    async with sessions() as session:
        return (await session.execute(select(AuditLog))).scalar_one()


# --------------------------------------------------------------------------------------
# The columns are populated with real values
# --------------------------------------------------------------------------------------


async def test_audit_write_populates_real_signature_columns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _write_audit(sessions)
    row = await _fetch_audit(sessions)

    assert row.signature is not None and len(row.signature) > 0
    assert row.sig_alg == "ED25519"
    # `provider:version:backend_ref` — the format platform.auth.repository already uses.
    assert row.key_id is not None
    provider, version, backend_ref = row.key_id.split(":", 2)
    assert provider == "dev"
    assert int(version) >= 1
    assert backend_ref == "evidence_root__default"


async def test_a_freshly_written_audit_entry_verifies(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _write_audit(sessions)
    assert await _verify_audit(await _fetch_audit(sessions)) is True


async def test_custody_write_populates_real_signature_columns(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    evidence_id = uuid.uuid4()
    custody_event_id = uuid.uuid4()
    entry_hash = _custody_entry_hash(
        prev_hash="0" * 64,
        custody_event_id=custody_event_id,
        evidence_id=evidence_id,
        sequence_number=1,
        event_type="ingested",
        occurred_at=_NOW,
        actor_user_id=None,
        actor_role="system",
        authority_ref=None,
        notes=None,
        integrity_hash_at_event="a" * 64,
    )
    signature = await _signer().sign(
        ledger=LEDGER_CUSTODY, sequence=1, prev_hash="0" * 64, entry_hash=entry_hash
    )
    async with sessions() as session:
        session.add(
            Evidence(
                evidence_id=evidence_id,
                schema_version="1.0.0",
                category="digital_forensics",
                artifact_type="file_artifact",
                title="signature probe",
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
                notes=None,
                integrity_hash_at_event="a" * 64,
                prev_event_hash="0" * 64,
                entry_hash=entry_hash,
                hash_algo="SHA-256",
                preimage_version=1,
                signature=signature.envelope,
                sig_alg=signature.sig_alg,
                key_id=signature.key_id,
            )
        )
        await session.commit()

    async with sessions() as session:
        row = (await session.execute(select(EvidenceCustodyEvent))).scalar_one()

    assert row.sig_alg == "ED25519"
    assert await _signer().verify(
        ledger=LEDGER_CUSTODY,
        sequence=row.sequence_number,
        prev_hash=row.prev_event_hash,
        entry_hash=row.entry_hash,
        envelope=row.signature,
    )


# --------------------------------------------------------------------------------------
# The attack ADR-0003 exists to stop
# --------------------------------------------------------------------------------------


async def test_administrator_recomputing_the_hash_still_fails_signature_check(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of the increment, carried out end to end.

    A privileged insider edits the attribution on a stored row **and recomputes its entry hash**,
    exactly as the application would. Under Wave 1.2 alone this produced a row that verified
    perfectly: the hash covered the new contents and matched. The signature does not, because
    producing a valid one needs the private key, which lives in the KMS and not in the database.
    """
    await _write_audit(sessions)
    original = await _fetch_audit(sessions)
    assert await _verify_audit(original) is True  # honest to begin with

    forged_role, forged_ip = "admin", "10.0.0.99"
    recomputed = _compute_hash(
        prev_hash=original.prev_entry_hash,
        audit_id=original.audit_id,
        occurred_at=original.occurred_at,
        actor_user_id=original.actor_user_id,
        actor_role=forged_role,
        action=original.action,
        module=original.module,
        target_type=original.target_type,
        target_id=original.target_id,
        ip_address=forged_ip,
        user_agent=original.user_agent,
        details=original.details,
    )
    async with sessions() as session:
        await session.execute(
            text(
                "UPDATE platform.audit_log SET actor_role = :role, ip_address = :ip, "
                "entry_hash = :h WHERE audit_id = :id"
            ),
            {"role": forged_role, "ip": forged_ip, "h": recomputed, "id": original.audit_id},
        )
        await session.commit()

    forged = await _fetch_audit(sessions)
    assert forged.actor_role == forged_role
    # The hash now genuinely matches the forged contents — a hash-only verifier is satisfied.
    assert (
        _compute_hash(
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
        == forged.entry_hash
    )
    # ...and the signature is not. This is the line that closes SR-4.
    assert await _verify_audit(forged) is False


async def test_swapping_in_a_signature_from_another_entry_fails(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A valid signature is still only valid for the entry it was made over."""
    await _write_audit(sessions, action="first")
    first = await _fetch_audit(sessions)

    # A second entry, whose signature the attacker transplants onto the first.
    await _write_audit(sessions, action="second")
    async with sessions() as session:
        rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
        )
    second = rows[1]

    async with sessions() as session:
        await session.execute(
            text("UPDATE platform.audit_log SET signature = :sig WHERE audit_id = :id"),
            {"sig": second.signature, "id": first.audit_id},
        )
        await session.commit()

    async with sessions() as session:
        tampered = (
            await session.execute(select(AuditLog).where(AuditLog.audit_id == first.audit_id))
        ).scalar_one()
    assert await _verify_audit(tampered) is False


async def test_a_custody_signature_cannot_be_replayed_onto_the_audit_ledger(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Domain separation: the ledger name is inside the signed message, so a signature made for
    one ledger does not verify against the other even with identical hashes."""
    signer = _signer()
    custody_sig = await signer.sign(
        ledger=LEDGER_CUSTODY, sequence=None, prev_hash="0" * 64, entry_hash="a" * 64
    )
    assert (
        await signer.verify(
            ledger=LEDGER_CUSTODY,
            sequence=None,
            prev_hash="0" * 64,
            entry_hash="a" * 64,
            envelope=custody_sig.envelope,
        )
        is True
    )
    assert (
        await signer.verify(
            ledger=LEDGER_AUDIT,
            sequence=None,
            prev_hash="0" * 64,
            entry_hash="a" * 64,
            envelope=custody_sig.envelope,
        )
        is False
    )


async def test_clearing_the_signature_column_does_not_pass_verification(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Stripping the signature must not be a way to make an entry acceptable.

    The obvious attack against "verify a signature if one is present" is to remove it. An unsigned
    entry is not authentic, so verification returns False — the Verification Engine distinguishes
    *unsigned* from *forged* by checking the column itself, because on a court-facing report those
    are different findings.
    """
    await _write_audit(sessions)
    row = await _fetch_audit(sessions)
    async with sessions() as session:
        await session.execute(
            text("UPDATE platform.audit_log SET signature = NULL WHERE audit_id = :id"),
            {"id": row.audit_id},
        )
        await session.commit()

    stripped = await _fetch_audit(sessions)
    assert stripped.signature is None
    assert await _verify_audit(stripped) is False


async def test_rewriting_the_recorded_algorithm_does_not_change_what_is_verified(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """`sig_alg` and `key_id` are denormalized conveniences, not verification inputs.

    They exist so operations can query "what was signed under the key version we are retiring".
    Verification reads the envelope's own copies, which are covered by the signature — so lying in
    the columns changes nothing, and lying in the envelope invalidates it.
    """
    await _write_audit(sessions)
    row = await _fetch_audit(sessions)
    async with sessions() as session:
        await session.execute(
            text(
                "UPDATE platform.audit_log SET sig_alg = 'ECDSA_P256', key_id = 'dev:99:bogus' "
                "WHERE audit_id = :id"
            ),
            {"id": row.audit_id},
        )
        await session.commit()

    lied_about = await _fetch_audit(sessions)
    assert lied_about.sig_alg == "ECDSA_P256"
    assert await _verify_audit(lied_about) is True  # unchanged: the envelope is what counts


async def test_a_corrupted_envelope_is_invalid_rather_than_an_error(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Garbage in the signature column is an invalid signature, not a crash."""
    await _write_audit(sessions)
    row = await _fetch_audit(sessions)
    async with sessions() as session:
        await session.execute(
            text("UPDATE platform.audit_log SET signature = :sig WHERE audit_id = :id"),
            {"sig": b"not-an-envelope", "id": row.audit_id},
        )
        await session.commit()

    assert await _verify_audit(await _fetch_audit(sessions)) is False


async def test_a_chain_of_audit_entries_each_verifies(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Three real writes through the production path: linked, hashed, and individually signed."""
    for index in range(3):
        await _write_audit(sessions, action=f"action.{index}", details={"i": index})

    async with sessions() as session:
        rows = list(
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars()
        )
    assert len(rows) == 3
    expected_prev = "0" * 64
    for row in rows:
        assert row.prev_entry_hash == expected_prev
        assert await _verify_audit(row) is True
        expected_prev = row.entry_hash
