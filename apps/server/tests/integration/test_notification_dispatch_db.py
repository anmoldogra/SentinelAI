"""The notification dispatch path, end to end against real Postgres — §25.9, api-design.md §8.

One chain, no fakes in the middle of it: a signed event goes into the *producing* module's outbox,
the **real dispatcher** claims and verifies it, `notification`'s real handler claims the inbox and
calls the real service, the logging sender records the delivery, and the notification, its delivery
row and `notification.dispatched` all land in one transaction.

What this file exists to prove, beyond "a message gets created":

* **both layers of §25.9's idempotency.** The Inbox claim stops the *same* event acting twice; the
  business key stops *two different* events describing one fact producing two messages. §25.9 calls
  this "the tightest idempotency key in the catalog, since a replayed event must never re-send an
  email the analyst already received", so both halves are asserted, not assumed.
* **that the key is enforced by the database, not only checked.** `exists_for_source` is a
  read-then-write and cannot survive two concurrent workers; `uq_notification_dedupe` can. The test
  inserts the duplicate directly, which is what a lost race looks like.
* **that `case.status_changed`'s wider key still discriminates** — a case notifies once per
  transition, not once per case, which a naive triple-only key would break.
* the admin surface: rule CRUD writes real audit entries, the ETag guard holds, and `redeliver` adds
  an attempt without ever adding a second notification.

Runs in a throwaway database created and dropped here. Skips cleanly when no Postgres is reachable;
never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.entrypoints.http.exception_handlers import register_exception_handlers
from sentinelai.entrypoints.http.middleware import register_middleware
from sentinelai.modules.notification import events as notification_events
from sentinelai.modules.notification.models import (
    Notification,
    NotificationDelivery,
    NotificationRule,
)
from sentinelai.modules.notification.router import router as notification_router
from sentinelai.modules.notification.service import DELIVERY_DELIVERED, DELIVERY_FAILED
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.db.base import Base
from sentinelai.platform.db.session import get_session
from sentinelai.platform.events.dispatcher import VERIFY_STRICT, EventDispatcher
from sentinelai.platform.events.inbox import get_inbox_table
from sentinelai.platform.events.outbox import OutboxWriter, get_outbox_table
from sentinelai.platform.events.signing import EventSigner
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
# The producing schemas hold only their outbox: `notification` is a terminal consumer and needs the
# event, never the row behind it.
_PRODUCERS = ("investigation", "case_management", "ingestion")
_SCHEMAS = ("platform", "notification", *_PRODUCERS)

_ADMIN = CurrentUser(user_id=uuid4(), roles=("admin",))
_INVESTIGATOR = uuid4()
_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


# --- database plumbing ------------------------------------------------------
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


async def _create_throwaway_database() -> tuple[str, str]:
    name = f"sentinelai_notif_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    return name, _URL.rsplit("/", 1)[0] + f"/{name}"


async def _drop_throwaway_database(name: str) -> None:
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def _create_tables(engine: AsyncEngine) -> None:
    """The three notification tables, the audit log, and one outbox per producing schema.

    Created from the ORM metadata rather than by running Alembic so a failure here is a missing
    table
    rather than a migration ordering problem three modules away. ``uq_notification_dedupe`` is
    declared on the model precisely so ``create_all`` reproduces it — the race assertion below is
    meaningless without it.
    """
    async with engine.begin() as conn:
        for schema in _SCHEMAS:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                AuditLog.__table__,
                NotificationRule.__table__,
                Notification.__table__,
                NotificationDelivery.__table__,
            ],
        )
        await conn.run_sync(get_outbox_table("notification").create, checkfirst=True)
        await conn.run_sync(get_inbox_table("notification").create, checkfirst=True)
        for schema in _PRODUCERS:
            await conn.run_sync(get_outbox_table(schema).create, checkfirst=True)


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )
    database, url = await _create_throwaway_database()
    engine = create_async_engine(url)
    try:
        await _create_tables(engine)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await _drop_throwaway_database(database)


# --- the production wiring, assembled --------------------------------------
def _dispatcher(db: async_sessionmaker[AsyncSession]) -> EventDispatcher:
    """The real relay, in strict signature mode, with notification's real registrations.

    ``VERIFY_STRICT`` is the point of using the real thing: an event whose signature does not verify
    never reaches a handler (ADR-0007 §2). So every assertion below that a notification exists is
    also
    an assertion that the event behind it was verified, not merely present.
    """
    dispatcher = EventDispatcher(
        db,
        poll_schemas=_PRODUCERS,
        lease_seconds=0,
        signer=EventSigner(kms_for_tests()),
        signature_mode=VERIFY_STRICT,
    )
    notification_events.register_consumers(dispatcher)
    return dispatcher


async def _drain(dispatcher: EventDispatcher, *, passes: int = 4) -> None:
    for _ in range(passes):
        if await dispatcher._poll_once() == 0:
            return


async def _publish(
    db: async_sessionmaker[AsyncSession],
    *,
    schema: str,
    event_type: str,
    aggregate_id: UUID,
    payload: dict[str, Any],
) -> None:
    """Write one signed event into a producing module's outbox, exactly as that module would."""
    async with db() as session:
        await OutboxWriter(session, schema=schema, signer=EventSigner(kms_for_tests())).publish(
            event_type=event_type,
            aggregate_type="test",
            aggregate_id=aggregate_id,
            payload=payload,
            correlation_id=str(uuid4()),
            actor_type="system",
        )
        await session.commit()


def _app(db: async_sessionmaker[AsyncSession]) -> FastAPI:
    """Just the notification router, on the same throwaway database."""

    async def _session() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            yield session

    application = FastAPI()
    register_middleware(application)
    register_exception_handlers(application)
    application.include_router(notification_router)
    application.dependency_overrides[get_current_user] = lambda: _ADMIN
    application.dependency_overrides[get_kms] = lambda: kms_for_tests()
    application.dependency_overrides[get_session] = _session
    return application


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _rows(db: async_sessionmaker[AsyncSession], model: Any) -> list[Any]:
    async with db() as session:
        return list((await session.execute(select(model))).scalars().all())


async def _count(db: async_sessionmaker[AsyncSession], model: Any, pk: Any) -> int:
    async with db() as session:
        return int((await session.execute(select(func.count(pk)))).scalar_one())


async def _events(db: async_sessionmaker[AsyncSession], schema: str) -> list[Any]:
    table = get_outbox_table(schema)
    async with db() as session:
        return list((await session.execute(select(table))).mappings().all())


async def _reset_for_redelivery(db: async_sessionmaker[AsyncSession], schema: str) -> None:
    """Make every dispatched event pending again — a crash between handler and mark, or a replay.

    The Inbox rows are deliberately left alone: that is what makes this a *redelivery* rather than a
    replay, and the inbox claim is the layer under test.
    """
    table = get_outbox_table(schema)
    async with db() as session:
        await session.execute(
            table.update().values(dispatch_status="pending", last_attempted_at=None)
        )
        await session.commit()


# --- the dispatch path ------------------------------------------------------
async def test_a_correlation_finding_reaches_the_investigator(db) -> None:
    """§25.9: "Create and dispatch a notification to the case's assigned investigator(s)"."""
    relationship_id, case_id = uuid4(), uuid4()

    await _publish(
        db,
        schema="investigation",
        event_type="investigation.correlation_generated",
        aggregate_id=relationship_id,
        payload={
            "case_id": str(case_id),
            "relationship_id": str(relationship_id),
            "entity_id": None,
            "confidence": "0.500",
            "generated_by": "heuristic-identifier-extraction/1 run:test",
            "recipient_user_id": str(_INVESTIGATOR),
        },
    )
    await _drain(_dispatcher(db))

    [notification] = await _rows(db, Notification)
    assert notification.recipient_user_id == _INVESTIGATOR
    assert notification.source_module == "investigation"
    assert notification.source_reference_id == relationship_id
    assert notification.read_at is None
    assert str(relationship_id) in (notification.message or "")

    [delivery] = await _rows(db, NotificationDelivery)
    assert delivery.delivery_status == DELIVERY_DELIVERED
    assert delivery.notification_id == notification.notification_id
    assert delivery.delivered_at is not None

    published = [e["event_type"] for e in await _events(db, "notification")]
    assert published == ["notification.dispatched"]


async def test_redelivering_the_same_event_creates_nothing_new(db) -> None:
    """**Layer one of §25.9's idempotency: the Inbox claim.** The same event, delivered twice.

    This is what a worker crash between the handler's commit and the outbox mark looks like, and
    what
    §17 says every handler must survive.
    """
    relationship_id = uuid4()
    await _publish(
        db,
        schema="investigation",
        event_type="investigation.correlation_generated",
        aggregate_id=relationship_id,
        payload={
            "case_id": str(uuid4()),
            "relationship_id": str(relationship_id),
            "recipient_user_id": str(_INVESTIGATOR),
        },
    )
    dispatcher = _dispatcher(db)
    await _drain(dispatcher)

    await _reset_for_redelivery(db, "investigation")
    await _drain(dispatcher)

    # The event really was claimed a second time: it was reset to `pending` above, and only a second
    # pass through the dispatcher could have marked it `dispatched` again. Without this the
    # assertions below could pass vacuously, on a drain that found nothing to do.
    [relayed] = await _events(db, "investigation")
    assert relayed["dispatch_status"] == "dispatched"

    assert await _count(db, Notification, Notification.notification_id) == 1
    assert await _count(db, NotificationDelivery, NotificationDelivery.delivery_id) == 1
    assert len(await _events(db, "notification")) == 1


async def test_two_events_for_one_finding_notify_once(db) -> None:
    """**Layer two: the business key.** Two *different* events (different `event_id`s) describing
    the
    same fact — a replayed upstream publication, or one finding announced twice.

    The Inbox cannot help here: both events are genuinely new. Only
    ``(recipient_user_id, source_module, source_reference_id)`` can, and §25.9 is explicit that it
    must — "a replayed event must never re-send an email the analyst already received".
    """
    relationship_id, case_id = uuid4(), uuid4()
    payload = {
        "case_id": str(case_id),
        "relationship_id": str(relationship_id),
        "recipient_user_id": str(_INVESTIGATOR),
    }
    for _ in range(2):
        await _publish(
            db,
            schema="investigation",
            event_type="investigation.correlation_generated",
            aggregate_id=relationship_id,
            payload=payload,
        )
    await _drain(_dispatcher(db))

    assert len(await _events(db, "investigation")) == 2  # two distinct events really were relayed
    assert await _count(db, Notification, Notification.notification_id) == 1
    assert await _count(db, NotificationDelivery, NotificationDelivery.delivery_id) == 1


async def test_the_same_finding_for_two_recipients_notifies_each(db) -> None:
    """The key is per recipient, so two investigators on one finding each get told once.

    §25.9 keys on the recipient for exactly this reason — a shared key would silence the second
    analyst.
    """
    relationship_id = uuid4()
    second_investigator = uuid4()
    for recipient in (_INVESTIGATOR, second_investigator):
        await _publish(
            db,
            schema="investigation",
            event_type="investigation.correlation_generated",
            aggregate_id=relationship_id,
            payload={
                "case_id": str(uuid4()),
                "relationship_id": str(relationship_id),
                "recipient_user_id": str(recipient),
            },
        )
    await _drain(_dispatcher(db))

    recipients = {n.recipient_user_id for n in await _rows(db, Notification)}
    assert recipients == {_INVESTIGATOR, second_investigator}


async def test_the_database_refuses_a_duplicate_the_check_could_race(db) -> None:
    """**What the unique index is for, and why the service check is not enough.**

    ``exists_for_source`` is a read-then-write: two dispatcher workers handling two different events
    about one fact can both find nothing and both insert, and the analyst gets the message twice —
    precisely what §25.9 forbids. Inserting the duplicate directly is what that lost race looks like
    from the database's point of view.
    """
    relationship_id = uuid4()
    await _publish(
        db,
        schema="investigation",
        event_type="investigation.correlation_generated",
        aggregate_id=relationship_id,
        payload={
            "case_id": str(uuid4()),
            "relationship_id": str(relationship_id),
            "recipient_user_id": str(_INVESTIGATOR),
        },
    )
    await _drain(_dispatcher(db))
    [existing] = await _rows(db, Notification)

    async with db() as session:
        session.add(
            Notification(
                rule_id=None,
                recipient_user_id=existing.recipient_user_id,
                source_module=existing.source_module,
                source_reference_id=existing.source_reference_id,
                message=existing.message,
                created_at=_NOW,
                read_at=None,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    assert await _count(db, Notification, Notification.notification_id) == 1


async def test_a_notification_with_no_source_is_not_deduplicated(db) -> None:
    """The other half of the index's behaviour, and it is the right answer rather than a loophole.

    §3.6 makes ``source_module``/``source_reference_id`` nullable, Postgres treats each NULL as
    distinct in a unique index, and a row describing no upstream fact has no business key to be
    deduplicated on.
    """
    async with db() as session:
        for _ in range(2):
            session.add(
                Notification(
                    rule_id=None,
                    recipient_user_id=_INVESTIGATOR,
                    source_module=None,
                    source_reference_id=None,
                    message="A message with no upstream fact behind it.",
                    created_at=_NOW,
                    read_at=None,
                )
            )
        await session.commit()

    assert await _count(db, Notification, Notification.notification_id) == 2


# --- the wider key: case.status_changed ------------------------------------
async def test_each_case_transition_notifies_once(db) -> None:
    """§25.9's key here is ``(recipient_user_id, source_reference_id, new_status)``.

    **A triple-only key would break this**: a case legitimately notifies its investigator on every
    transition, so keying on ``(recipient, case_management, case_id)`` alone would report "open" and
    then silence "closed" forever. The service carries `new_status` by matching the stored message,
    which it composes as a pure function of exactly the key fields.
    """
    case_id = uuid4()
    for new_status in ("under_review", "closed"):
        await _publish(
            db,
            schema="case_management",
            event_type="case.status_changed",
            aggregate_id=case_id,
            payload={
                "case_id": str(case_id),
                "previous_status": "open",
                "new_status": new_status,
                "owning_user_id": str(_INVESTIGATOR),
                "changed_at": _NOW.isoformat(),
            },
        )
    await _drain(_dispatcher(db))

    notifications = await _rows(db, Notification)
    assert len(notifications) == 2
    assert {n.source_reference_id for n in notifications} == {case_id}
    assert len({n.message for n in notifications}) == 2


async def test_reaching_the_same_status_twice_notifies_once(db) -> None:
    """The same key's other half: re-entering a status is not a new fact to tell the analyst."""
    case_id = uuid4()
    for _ in range(2):
        await _publish(
            db,
            schema="case_management",
            event_type="case.status_changed",
            aggregate_id=case_id,
            payload={
                "case_id": str(case_id),
                "previous_status": "open",
                "new_status": "under_review",
                "owning_user_id": str(_INVESTIGATOR),
                "changed_at": _NOW.isoformat(),
            },
        )
    await _drain(_dispatcher(db))

    assert await _count(db, Notification, Notification.notification_id) == 1


async def test_a_generated_report_notifies_its_requester(db) -> None:
    """§25.9 keys this on the report, so regenerating a case's report is a genuinely new fact."""
    case_id = uuid4()
    first_report, second_report = uuid4(), uuid4()
    for report_id in (first_report, second_report):
        await _publish(
            db,
            schema="case_management",
            event_type="case.report_generated",
            aggregate_id=report_id,
            payload={
                "case_id": str(case_id),
                "report_id": str(report_id),
                "requested_by_user_id": str(_INVESTIGATOR),
            },
        )
    await _drain(_dispatcher(db))

    assert {n.source_reference_id for n in await _rows(db, Notification)} == {
        first_report,
        second_report,
    }


async def test_an_event_naming_no_recipient_is_consumed_and_ignored(db) -> None:
    """The handler cannot invent somebody to notify, and dead-lettering an otherwise valid upstream
    fact would be worse than sending nothing."""
    relationship_id = uuid4()
    await _publish(
        db,
        schema="investigation",
        event_type="investigation.correlation_generated",
        aggregate_id=relationship_id,
        payload={"case_id": str(uuid4()), "relationship_id": str(relationship_id)},
    )
    await _drain(_dispatcher(db))

    assert await _rows(db, Notification) == []
    # Consumed, not dead-lettered: the event is marked dispatched.
    assert {e["dispatch_status"] for e in await _events(db, "investigation")} == {"dispatched"}


async def test_a_correlation_run_failure_notifies_nobody(db) -> None:
    """**The documented §25 discrepancy, pinned as behaviour.**

    §25.8's publisher table names `notification` as the consumer of
    `investigation.correlation_run_failed`; §25.9's Consumed table has no row for it, so there is no
    documented handler action, idempotency key or retry policy to build against. No handler is
    registered, and §23 permits exactly that — "the dispatcher itself must never crash on an event
    type it has no registered handler for".

    Asserted so the gap is a known, tested behaviour rather than a surprise: the event relays
    cleanly
    and nobody is told. A future increment that resolves the documentation will make this test fail,
    which is the correct way to find out.
    """
    run_id = uuid4()
    await _publish(
        db,
        schema="investigation",
        event_type="investigation.correlation_run_failed",
        aggregate_id=run_id,
        payload={
            "run_id": str(run_id),
            "case_id": str(uuid4()),
            "findings_generated_count": 0,
        },
    )
    await _drain(_dispatcher(db))

    assert await _rows(db, Notification) == []
    assert {e["dispatch_status"] for e in await _events(db, "investigation")} == {"dispatched"}


# --- the admin surface over HTTP -------------------------------------------
async def test_rule_crud_round_trips_and_is_audited(db) -> None:
    """§4.9's three rule endpoints, plus the audit entries §22's insider threat model wants."""
    target = uuid4()

    async with await _client(_app(db)) as client:
        created = await client.post(
            "/api/v1/notification-rules",
            json={
                "name": "Alert supervisor on findings",
                "trigger_event_type": "investigation.correlation_generated",
                "channel": "log",
                "target_role_or_user": str(target),
            },
        )
        assert created.status_code == 201
        rule_id = created.json()["data"]["rule_id"]
        assert created.json()["data"]["is_active"] is True

        listed = await client.get("/api/v1/notification-rules")
        assert listed.status_code == 200
        assert [r["rule_id"] for r in listed.json()["data"]] == [rule_id]

        etag = f'W/"{uuid4().hex[:32]}"'
        stale = await client.patch(
            f"/api/v1/notification-rules/{rule_id}",
            json={"is_active": False},
            headers={"If-Match": etag},
        )
        assert stale.status_code == 412

    # The rule is untouched by the refused PATCH.
    [rule] = await _rows(db, NotificationRule)
    assert rule.is_active is True

    actions = {entry.action for entry in await _rows(db, AuditLog)}
    assert actions == {"notification_rule.created"}


async def test_a_rule_can_be_deactivated_with_a_fresh_etag(db) -> None:
    """The ETag the PATCH response carries is what makes a second change possible — §8 documents no
    single-rule GET, so without it a client has no in-contract way to obtain one."""
    async with await _client(_app(db)) as client:
        created = await client.post(
            "/api/v1/notification-rules",
            json={
                "name": "Alert on status changes",
                "trigger_event_type": "case.status_changed",
                "channel": "log",
                "target_role_or_user": str(uuid4()),
            },
        )
        rule_id = created.json()["data"]["rule_id"]

        # The list is where a client learns the rule exists; the PATCH response hands back the ETag
        # for the next change. Seed the first `If-Match` from the service's own digest.
        from sentinelai.modules.notification.service import rule_etag

        [rule] = await _rows(db, NotificationRule)
        first = await client.patch(
            f"/api/v1/notification-rules/{rule_id}",
            json={"name": "Renamed"},
            headers={"If-Match": rule_etag(rule)},
        )
        assert first.status_code == 200
        assert "ETag" in first.headers

        second = await client.patch(
            f"/api/v1/notification-rules/{rule_id}",
            json={"is_active": False},
            headers={"If-Match": first.headers["ETag"]},
        )
        assert second.status_code == 200
        assert second.json()["data"]["is_active"] is False

    actions = [entry.action for entry in await _rows(db, AuditLog)]
    assert actions.count("notification_rule.updated") == 2


async def test_a_rule_on_an_unpublished_event_is_refused_over_http(db) -> None:
    """422 — the request is well-formed and the value fails a domain rule (§2.4's split)."""
    async with await _client(_app(db)) as client:
        response = await client.post(
            "/api/v1/notification-rules",
            json={
                "name": "Alert on nothing",
                "trigger_event_type": "investigation.hypothesis_generated",
                "channel": "log",
                "target_role_or_user": str(uuid4()),
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert await _rows(db, NotificationRule) == []


async def test_the_inbox_filters_on_read_state(db) -> None:
    """§8's documented `read` filter, over HTTP, scoped to the caller."""
    async with db() as session:
        session.add(
            Notification(
                rule_id=None,
                recipient_user_id=_ADMIN.user_id,
                source_module="investigation",
                source_reference_id=uuid4(),
                message="unread",
                created_at=_NOW,
                read_at=None,
            )
        )
        session.add(
            Notification(
                rule_id=None,
                recipient_user_id=_ADMIN.user_id,
                source_module="investigation",
                source_reference_id=uuid4(),
                message="already read",
                created_at=_NOW,
                read_at=_NOW,
            )
        )
        await session.commit()

    async with await _client(_app(db)) as client:
        unread = await client.get("/api/v1/notifications", params={"read": "false"})
        read = await client.get("/api/v1/notifications", params={"read": "true"})
        both = await client.get("/api/v1/notifications")

    assert [n["message"] for n in unread.json()["data"]] == ["unread"]
    assert [n["message"] for n in read.json()["data"]] == ["already read"]
    assert len(both.json()["data"]) == 2


# --- redelivery ------------------------------------------------------------
async def _seed_failed_notification(db: async_sessionmaker[AsyncSession]) -> UUID:
    """One notification whose only delivery attempt failed — what an operator retries."""
    notification_id = uuid4()
    async with db() as session:
        session.add(
            Notification(
                notification_id=notification_id,
                rule_id=None,
                recipient_user_id=_INVESTIGATOR,
                source_module="investigation",
                source_reference_id=uuid4(),
                message="A new relationship was proposed for your case.",
                created_at=_NOW,
                read_at=None,
            )
        )
        session.add(
            NotificationDelivery(
                notification_id=notification_id,
                channel="log",
                delivery_status=DELIVERY_FAILED,
                attempted_at=_NOW,
                delivered_at=None,
            )
        )
        await session.commit()
    return notification_id


async def test_redelivery_adds_an_attempt_not_a_notification(db) -> None:
    """**The distinction that keeps this endpoint from being a way around §25.9.** However many
    times
    an admin retries, the analyst's inbox holds one entry for the fact."""
    notification_id = await _seed_failed_notification(db)

    async with await _client(_app(db)) as client:
        response = await client.post(f"/api/v1/notifications/{notification_id}/redeliver")

    assert response.status_code == 202
    assert response.json()["data"]["delivery_status"] == DELIVERY_DELIVERED
    assert await _count(db, Notification, Notification.notification_id) == 1
    assert await _count(db, NotificationDelivery, NotificationDelivery.delivery_id) == 2
    assert {e.action for e in await _rows(db, AuditLog)} == {"notification.redelivered"}


async def test_redelivering_a_delivered_notification_is_refused(db) -> None:
    """§4.9 defines the endpoint as retrying a *failed* delivery. Re-sending a delivered one is the
    duplicate §25.9's tightest key exists to prevent."""
    notification_id = await _seed_failed_notification(db)

    async with await _client(_app(db)) as client:
        first = await client.post(f"/api/v1/notifications/{notification_id}/redeliver")
        second = await client.post(f"/api/v1/notifications/{notification_id}/redeliver")

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"
    # The refusal added nothing.
    assert await _count(db, NotificationDelivery, NotificationDelivery.delivery_id) == 2


async def test_redelivering_a_missing_notification_is_a_404(db) -> None:
    async with await _client(_app(db)) as client:
        response = await client.post(f"/api/v1/notifications/{uuid4()}/redeliver")

    assert response.status_code == 404
