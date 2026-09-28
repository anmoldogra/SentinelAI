"""Event authentication end to end — ADR-0007, Wave 2.3.

The claim under test is that a **forged outbox row never reaches a handler**. That has to be proven
against a real database, because the attack is a real ``INSERT``: an insider with write access to a
module's schema writing a domain fact by hand. A unit test with a fake session cannot express that —
the forgery *is* the row.

Real Ed25519 from the dev KMS provider throughout. A stub signer would make every rejection
assertion vacuous: the whole point is that a signature which does not verify is rejected, and only
real asymmetric crypto can demonstrate it.

The three outcomes are tested separately because each fails differently, and the middle one is the
reason a single boolean on the signature would be too coarse:

* **verified** — delivered.
* **absent** — delivered only under ``permissive`` (a row written before Wave 2.3).
* **invalid** — never delivered, in either mode. Tolerating a present-but-bad signature under
  permissive would hand an attacker a downgrade: corrupt the envelope and the forgery is treated as
  merely unsigned.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.config import settings
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.dispatcher import (
    VERIFY_PERMISSIVE,
    VERIFY_STRICT,
    EventDispatcher,
)
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.outbox import OutboxWriter, get_outbox_table
from sentinelai.platform.events.signing import EventSigner
from tests.fixtures.kms import foreign_kms_for_tests, kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_SCHEMA = "ingestion"
_OTHER_SCHEMA = "case_management"
_EVENT_TYPE = "evidence.ingested"


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
    """A throwaway database with two modules' outbox tables (for the cross-schema replay test)."""
    if not await _reachable(_URL):
        pytest.skip(f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL")

    name = f"sentinelai_evtsig_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    engine: AsyncEngine = create_async_engine(_URL.rsplit("/", 1)[0] + f"/{name}")
    try:
        async with engine.begin() as conn:
            for schema in (_SCHEMA, _OTHER_SCHEMA):
                await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
                await conn.run_sync(get_outbox_table(schema).create)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


class _Recorder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle(self, event: EventEnvelope, _uow: UnitOfWork) -> None:
        self.seen.append(str(event.payload.get("marker")))


def _dispatcher(
    sessions: async_sessionmaker[AsyncSession],
    recorder: _Recorder,
    *,
    mode: str = VERIFY_STRICT,
    signer: EventSigner | None = None,
    schema: str = _SCHEMA,
) -> EventDispatcher:
    dispatcher = EventDispatcher(
        sessions,
        poll_schemas=(schema,),
        lease_seconds=0,
        signer=signer if signer is not None else EventSigner(kms_for_tests()),
        signature_mode=mode,
    )
    dispatcher.register(_EVENT_TYPE, recorder.handle, inbox_schema=schema)
    return dispatcher


async def _publish_signed(
    sessions: async_sessionmaker[AsyncSession], marker: str, *, schema: str = _SCHEMA
) -> uuid.UUID:
    """Publish through the real writer, so the row is signed exactly as production signs it."""
    async with sessions() as session:
        writer = OutboxWriter(session, schema=schema, signer=EventSigner(kms_for_tests()))
        await writer.publish(
            event_type=_EVENT_TYPE,
            aggregate_type="evidence",
            aggregate_id=uuid.uuid4(),
            payload={"marker": marker},
            correlation_id=str(uuid.uuid4()),
            actor_type="user",
        )
        await session.commit()
    return await _event_id_of(sessions, marker, schema=schema)


async def _insert_unsigned(
    sessions: async_sessionmaker[AsyncSession], marker: str, *, schema: str = _SCHEMA
) -> uuid.UUID:
    """A hand-written row: exactly the forgery ADR-0007's Context describes."""
    table = get_outbox_table(schema)
    event_id = uuid.uuid4()
    async with sessions() as session:
        await session.execute(
            insert(table).values(
                event_id=event_id,
                event_type=_EVENT_TYPE,
                event_version="1.0.0",
                aggregate_type="evidence",
                aggregate_id=uuid.uuid4(),
                payload={"marker": marker},
                correlation_id=uuid.uuid4(),
                causation_id=None,
                trace_id=None,
                actor_type="user",
                actor_ref=None,
                occurred_at=datetime.now(UTC),
                dispatch_status="pending",
                attempt_count=0,
                last_error=None,
                last_attempted_at=None,
                signature=None,
                key_id=None,
                sig_alg=None,
            )
        )
        await session.commit()
    return event_id


async def _event_id_of(
    sessions: async_sessionmaker[AsyncSession], marker: str, *, schema: str = _SCHEMA
) -> uuid.UUID:
    table = get_outbox_table(schema)
    async with sessions() as session:
        row = (
            await session.execute(
                select(table.c.event_id).where(table.c.payload["marker"].astext == marker)
            )
        ).one()
    return uuid.UUID(str(row[0]))


async def _row_state(
    sessions: async_sessionmaker[AsyncSession], marker: str, *, schema: str = _SCHEMA
) -> tuple[str, str | None]:
    table = get_outbox_table(schema)
    async with sessions() as session:
        row = (
            await session.execute(
                select(table.c.dispatch_status, table.c.last_error).where(
                    table.c.payload["marker"].astext == marker
                )
            )
        ).one()
    return str(row[0]), (str(row[1]) if row[1] is not None else None)


# ---------------------------------------------------------------------------------------
# Publishing signs — without this, every rejection below could be passing for the wrong reason
# ---------------------------------------------------------------------------------------


async def test_publishing_writes_a_signature_key_id_and_algorithm(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _publish_signed(sessions, "signed")

    table = get_outbox_table(_SCHEMA)
    async with sessions() as session:
        row = (
            await session.execute(
                select(table.c.signature, table.c.key_id, table.c.sig_alg).where(
                    table.c.payload["marker"].astext == "signed"
                )
            )
        ).one()

    assert row[0], "a signature envelope must be persisted"
    assert "ED25519" in str(row[2]), f"expected an Ed25519 signature, got {row[2]!r}"
    assert str(row[1]).startswith("dev:"), f"key_id must name the provider/version, got {row[1]!r}"


async def test_a_genuinely_signed_event_is_delivered(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The baseline. Every negative assertion in this file depends on this passing."""
    await _publish_signed(sessions, "authentic")
    recorder = _Recorder()

    await _dispatcher(sessions, recorder)._poll_once()

    assert recorder.seen == ["authentic"]
    assert (await _row_state(sessions, "authentic"))[0] == "dispatched"


async def test_an_unsigned_publisher_writes_a_null_signature(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A writer with no signing identity must write NULL, not a placeholder.

    NULL is what lets a verifier distinguish "never signed" from "signature fails", which is the
    distinction permissive mode is built on.
    """
    async with sessions() as session:
        await OutboxWriter(session, schema=_SCHEMA).publish(
            event_type=_EVENT_TYPE,
            aggregate_type="evidence",
            aggregate_id=uuid.uuid4(),
            payload={"marker": "unsigned-writer"},
            correlation_id=str(uuid.uuid4()),
            actor_type="user",
        )
        await session.commit()

    table = get_outbox_table(_SCHEMA)
    async with sessions() as session:
        row = (
            await session.execute(
                select(table.c.signature, table.c.key_id, table.c.sig_alg).where(
                    table.c.payload["marker"].astext == "unsigned-writer"
                )
            )
        ).one()

    assert row == (None, None, None)


# ---------------------------------------------------------------------------------------
# Forgery — the attack ADR-0007 exists for
# ---------------------------------------------------------------------------------------


async def test_a_hand_written_event_is_never_delivered_under_strict(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The forgery in ADR-0007's Context: an insider INSERTs a domain fact.

    The Inbox cannot help — it deduplicates on ``(event_id, handler_name)`` and the forger minted a
    fresh id. Only the signature stops this, and it must stop it *before* the handler runs.
    """
    await _insert_unsigned(sessions, "forged")
    recorder = _Recorder()

    await _dispatcher(sessions, recorder, mode=VERIFY_STRICT)._poll_once()

    assert recorder.seen == [], "a forged event must never reach a handler"
    status, last_error = await _row_state(sessions, "forged")
    assert status == "dead_letter", "a rejected event is quarantined, not left to retry"
    assert last_error is not None and "ADR-0007" in last_error


async def test_a_tampered_payload_is_detected_even_though_the_signature_is_present(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The subtler attack: take a real signed event and edit what it says.

    The signature stays syntactically valid and the row looks complete. It no longer covers the
    payload, which is exactly what the verification catches.
    """
    await _publish_signed(sessions, "original")
    table = get_outbox_table(_SCHEMA)
    async with sessions() as session:
        await session.execute(
            update(table)
            .where(table.c.payload["marker"].astext == "original")
            .values(payload={"marker": "tampered"})
        )
        await session.commit()

    recorder = _Recorder()
    await _dispatcher(sessions, recorder)._poll_once()

    assert recorder.seen == []
    status, last_error = await _row_state(sessions, "tampered")
    assert status == "dead_letter"
    assert last_error is not None and "invalid" in last_error


async def test_rewriting_the_event_type_is_detected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Escalation by relabelling: keep the signature, change what the event claims to be."""
    await _publish_signed(sessions, "relabel")
    table = get_outbox_table(_SCHEMA)
    async with sessions() as session:
        await session.execute(
            update(table)
            .where(table.c.payload["marker"].astext == "relabel")
            .values(event_type="evidence.superseded")
        )
        await session.commit()

    recorder = _Recorder()
    dispatcher = _dispatcher(sessions, recorder)
    dispatcher.register("evidence.superseded", recorder.handle, inbox_schema=_SCHEMA)
    await dispatcher._poll_once()

    assert recorder.seen == []
    assert (await _row_state(sessions, "relabel"))[0] == "dead_letter"


async def test_a_corrupted_signature_envelope_is_rejected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Garbage in the signature column must be a rejection, not a crash or a skip."""
    await _publish_signed(sessions, "corrupt")
    table = get_outbox_table(_SCHEMA)
    async with sessions() as session:
        await session.execute(
            update(table)
            .where(table.c.payload["marker"].astext == "corrupt")
            .values(signature=b"not-a-signature-envelope")
        )
        await session.commit()

    recorder = _Recorder()
    await _dispatcher(sessions, recorder)._poll_once()

    assert recorder.seen == []
    assert (await _row_state(sessions, "corrupt"))[0] == "dead_letter"


async def test_an_event_copied_into_another_modules_outbox_is_rejected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The schema is a domain separator (ADR-0007 §1).

    A row lifted verbatim from ``ingestion.outbox_events`` into ``case_management.outbox_events``
    carries a genuine signature over genuine content — and must still be refused, because the
    signature was made for a different publisher's table.
    """
    await _publish_signed(sessions, "transplant", schema=_SCHEMA)
    source = get_outbox_table(_SCHEMA)
    target = get_outbox_table(_OTHER_SCHEMA)
    async with sessions() as session:
        row = (
            (
                await session.execute(
                    select(source).where(source.c.payload["marker"].astext == "transplant")
                )
            )
            .mappings()
            .one()
        )
        await session.execute(insert(target).values(**dict(row)))
        await session.commit()

    recorder = _Recorder()
    await _dispatcher(sessions, recorder, schema=_OTHER_SCHEMA)._poll_once()

    assert recorder.seen == [], "a signature must not validate in another module's schema"
    assert (await _row_state(sessions, "transplant", schema=_OTHER_SCHEMA))[0] == "dead_letter"


async def test_a_signature_from_a_different_key_is_rejected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Anyone can sign. Only the configured EVENT_ROOT key counts.

    Verified against a signer whose keystore is a different throwaway directory, so the envelope is
    real and correctly formed but made under a key this verifier does not trust.
    """
    await _publish_signed(sessions, "stranger-key")
    recorder = _Recorder()

    foreign = EventSigner(foreign_kms_for_tests())
    await _dispatcher(sessions, recorder, signer=foreign)._poll_once()

    assert recorder.seen == []
    assert (await _row_state(sessions, "stranger-key"))[0] == "dead_letter"


# ---------------------------------------------------------------------------------------
# The verify-optional rollback lever
# ---------------------------------------------------------------------------------------


async def test_permissive_mode_delivers_an_unsigned_legacy_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The migration window: rows written before Wave 2.3 have no signature and cannot gain one.

    Signing them now would attest to bytes nobody witnessed at publication, so permissive mode is
    what carries them through a release rather than a retroactive backfill.
    """
    await _insert_unsigned(sessions, "legacy")
    recorder = _Recorder()

    await _dispatcher(sessions, recorder, mode=VERIFY_PERMISSIVE)._poll_once()

    assert recorder.seen == ["legacy"]
    assert (await _row_state(sessions, "legacy"))[0] == "dispatched"


async def test_permissive_mode_still_refuses_an_invalid_signature(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The distinction that makes permissive safe to run at all.

    If permissive tolerated a present-but-invalid signature, an attacker would have a downgrade
    path: corrupt the envelope and the forgery is treated as merely unsigned. Absent is tolerated;
    invalid never is.
    """
    await _publish_signed(sessions, "bad-sig")
    table = get_outbox_table(_SCHEMA)
    async with sessions() as session:
        await session.execute(
            update(table)
            .where(table.c.payload["marker"].astext == "bad-sig")
            .values(payload={"marker": "bad-sig", "injected": True})
        )
        await session.commit()

    recorder = _Recorder()
    await _dispatcher(sessions, recorder, mode=VERIFY_PERMISSIVE)._poll_once()

    assert recorder.seen == []
    assert (await _row_state(sessions, "bad-sig"))[0] == "dead_letter"


async def test_a_dispatcher_with_no_signer_does_not_pretend_to_verify(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A relay with no KMS wired cannot verify, and must not silently claim events are authentic.

    It delivers (there is nothing to check with) and says so once at startup rather than per event.
    Asserted so the behaviour is a recorded decision rather than an accident of a None check.
    """
    await _insert_unsigned(sessions, "unverifiable")
    recorder = _Recorder()
    dispatcher = EventDispatcher(sessions, poll_schemas=(_SCHEMA,), lease_seconds=0, signer=None)
    dispatcher.register(_EVENT_TYPE, recorder.handle, inbox_schema=_SCHEMA)

    await dispatcher._poll_once()

    assert recorder.seen == ["unverifiable"]


async def test_a_rejected_event_is_not_retried(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A bad signature will not become good, so retrying would only delay the alarm."""
    await _insert_unsigned(sessions, "no-retry")
    recorder = _Recorder()
    dispatcher = _dispatcher(sessions, recorder)

    for _ in range(3):
        await dispatcher._poll_once()

    assert recorder.seen == []
    assert (await _row_state(sessions, "no-retry"))[0] == "dead_letter"
