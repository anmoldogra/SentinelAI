"""Unit tests for the notification module's ``evidence.scanned`` consumer.

Covers security-architecture §25's "notify the uploading analyst" on a malware block, and the
two-layer idempotency the §25.9 catalog requires: the Inbox claim (same event redelivered) and
the business key (a *different* event describing the same fact).

Driven through the real handler and the real service against in-memory fakes — the dispatcher and
the database are exercised elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinelai.modules.notification import events as notification_events
from sentinelai.modules.notification.events import on_evidence_scanned
from sentinelai.modules.notification.models import Notification
from sentinelai.modules.notification.service import NotificationService
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.notifications import LoggingNotificationSender, NotificationMessage

_EVIDENCE_ID = uuid4()
_UPLOADER = uuid4()


class _FakeNotificationRepo:
    def __init__(self) -> None:
        self.items: list[Notification] = []

    async def add(self, notification: Notification) -> None:
        if notification.notification_id is None:
            notification.notification_id = uuid4()
        self.items.append(notification)

    async def exists_for_source(
        self,
        recipient_user_id: UUID,
        source_module: str,
        source_reference_id: UUID,
        *,
        message: str | None = None,
    ) -> bool:
        return any(
            n.recipient_user_id == recipient_user_id
            and n.source_module == source_module
            and n.source_reference_id == source_reference_id
            and (message is None or n.message == message)
            for n in self.items
        )


class _FakeDeliveryRepo:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def add(self, delivery: Any) -> None:
        self.items.append(delivery)


class _FakeOutbox:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


class _FakeInbox:
    """Stands in for the real inbox_events table: an insert-first claim set."""

    def __init__(self) -> None:
        self.claims: set[tuple[UUID, str]] = set()
        self.processed: set[tuple[UUID, str]] = set()


class _FakeNotificationUow:
    def __init__(self, inbox: _FakeInbox) -> None:
        self.session = object()
        self.notifications = _FakeNotificationRepo()
        self.deliveries = _FakeDeliveryRepo()
        self.outbox = _FakeOutbox()
        self.commits = 0
        self.inbox = inbox

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def inbox() -> _FakeInbox:
    return _FakeInbox()


@pytest.fixture(autouse=True)
def _fake_inbox_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace InboxGuard with the fake claim-set, keeping insert-first semantics."""

    class _Guard:
        def __init__(self, session: Any, schema: str) -> None:
            self._store: _FakeInbox = session

        async def try_claim(self, event_id: UUID, handler_name: str) -> bool:
            key = (event_id, handler_name)
            if key in self._store.claims:
                return False
            self._store.claims.add(key)
            return True

        async def mark_processed(self, event_id: UUID, handler_name: str) -> None:
            self._store.processed.add((event_id, handler_name))

    monkeypatch.setattr(notification_events, "InboxGuard", _Guard)


def _uow(inbox: _FakeInbox) -> Any:
    uow = _FakeNotificationUow(inbox)
    uow.session = inbox  # the fake guard reads its claim set from "the session"
    return uow


def _event(**payload_overrides: Any) -> EventEnvelope:
    payload: dict[str, Any] = {
        "evidence_id": str(_EVIDENCE_ID),
        "category": "osint",
        "is_clean": False,
        "detection_name": "Eicar-Test-Signature",
        "promoted": False,
        "forensic_exception": False,
        "engine": "clamav",
        "collector_user_id": str(_UPLOADER),
    }
    payload.update(payload_overrides)
    return EventEnvelope(
        event_id=uuid4(),
        event_type="evidence.scanned",
        event_version="1.0.0",
        aggregate_type="evidence",
        aggregate_id=_EVIDENCE_ID,
        payload=payload,
        correlation_id=uuid4(),
        causation_id=None,
        trace_id=None,
        actor_type="system",
        actor_ref=None,
        occurred_at=datetime.now(UTC),
        dispatch_status="pending",
        attempt_count=0,
    )


# --- the blocked case: notify ----------------------------------------------


async def test_a_blocked_upload_notifies_the_uploading_analyst(inbox: _FakeInbox) -> None:
    uow = _uow(inbox)
    await on_evidence_scanned(_event(), uow)

    assert len(uow.notifications.items) == 1
    notification = uow.notifications.items[0]
    assert notification.recipient_user_id == _UPLOADER
    assert notification.source_module == "ingestion"
    assert notification.source_reference_id == _EVIDENCE_ID


async def test_the_message_names_the_detection_and_says_it_was_not_promoted(
    inbox: _FakeInbox,
) -> None:
    uow = _uow(inbox)
    await on_evidence_scanned(_event(), uow)
    message = uow.notifications.items[0].message or ""
    assert "Eicar-Test-Signature" in message
    assert "quarantine" in message
    assert "not promoted" in message


async def test_a_delivery_row_records_the_successful_dispatch(inbox: _FakeInbox) -> None:
    uow = _uow(inbox)
    await on_evidence_scanned(_event(), uow)
    delivery = uow.deliveries.items[0]
    assert delivery.delivery_status == "delivered"
    assert delivery.delivered_at is not None


async def test_a_dispatched_event_is_published_to_the_outbox(inbox: _FakeInbox) -> None:
    uow = _uow(inbox)
    await on_evidence_scanned(_event(), uow)
    published = [e["event_type"] for e in uow.outbox.published]
    assert published == ["notification.dispatched"]


async def test_an_unnamed_detection_still_produces_a_usable_message(inbox: _FakeInbox) -> None:
    uow = _uow(inbox)
    await on_evidence_scanned(_event(detection_name=None), uow)
    assert "unnamed detection" in (uow.notifications.items[0].message or "")


async def test_the_handler_never_commits(inbox: _FakeInbox) -> None:
    """ADR-0005: the dispatcher owns the handler's transaction."""
    uow = _uow(inbox)
    await on_evidence_scanned(_event(), uow)
    assert uow.commits == 0


# --- the ignored cases -------------------------------------------------------


async def test_a_clean_scan_notifies_nobody(inbox: _FakeInbox) -> None:
    uow = _uow(inbox)
    await on_evidence_scanned(_event(is_clean=True, detection_name=None, promoted=True), uow)
    assert uow.notifications.items == []
    assert uow.outbox.published == []


async def test_a_forensic_exception_promotion_notifies_nobody(inbox: _FakeInbox) -> None:
    """§25 carve-out: malware inside a forensic image is the evidence, not a failure."""
    uow = _uow(inbox)
    await on_evidence_scanned(
        _event(category="digital_forensics", promoted=True, forensic_exception=True), uow
    )
    assert uow.notifications.items == []


async def test_an_ignored_event_is_still_marked_processed(inbox: _FakeInbox) -> None:
    """Consumed-and-ignored must not look like unconsumed, or it would be redelivered forever."""
    uow = _uow(inbox)
    event = _event(is_clean=True, promoted=True)
    await on_evidence_scanned(event, uow)
    assert (event.event_id, "notification.on_evidence_scanned") in inbox.processed


async def test_a_payload_without_an_uploader_is_ignored_rather_than_crashing(
    inbox: _FakeInbox,
) -> None:
    uow = _uow(inbox)
    event = _event()
    del event.payload["collector_user_id"]
    await on_evidence_scanned(event, uow)
    assert uow.notifications.items == []


# --- idempotency: two independent layers ------------------------------------


async def test_redelivering_the_same_event_notifies_only_once(inbox: _FakeInbox) -> None:
    """Inbox claim (at-least-once delivery): the second pass short-circuits."""
    uow = _uow(inbox)
    event = _event()
    await on_evidence_scanned(event, uow)
    await on_evidence_scanned(event, uow)
    assert len(uow.notifications.items) == 1
    assert len(uow.outbox.published) == 1


async def test_a_different_event_for_the_same_evidence_notifies_only_once(
    inbox: _FakeInbox,
) -> None:
    """Business idempotency key (§25.9): a re-scan must not re-send a message already received."""
    uow = _uow(inbox)
    await on_evidence_scanned(_event(), uow)
    await on_evidence_scanned(_event(), uow)  # distinct event_id, same underlying fact
    assert len(uow.notifications.items) == 1


async def test_a_block_on_different_evidence_does_notify_again(inbox: _FakeInbox) -> None:
    """The key is scoped to the evidence — a genuinely new block must still reach the analyst."""
    uow = _uow(inbox)
    await on_evidence_scanned(_event(), uow)
    other = uuid4()
    await on_evidence_scanned(_event(evidence_id=str(other)), uow)
    assert len(uow.notifications.items) == 2


# --- delivery failure --------------------------------------------------------


async def test_a_channel_failure_keeps_the_notification_and_records_the_failure(
    inbox: _FakeInbox,
) -> None:
    """The in-app row is the durable delivery — a dead channel must not discard it."""

    class _BrokenSender:
        channel = "log"

        async def send(self, message: NotificationMessage) -> None:
            raise RuntimeError("channel down")

    uow = _uow(inbox)
    service = NotificationService(uow, sender=_BrokenSender())
    notification = await service.dispatch_for_evidence_scanned(
        evidence_id=_EVIDENCE_ID,
        recipient_user_id=_UPLOADER,
        detection_name="X",
        engine="clamav",
        correlation_id=str(uuid4()),
    )
    assert notification is not None
    assert uow.deliveries.items[0].delivery_status == "failed"
    assert [e["event_type"] for e in uow.outbox.published] == ["notification.delivery_failed"]


async def test_the_logging_sender_records_what_it_was_asked_to_send() -> None:
    sender = LoggingNotificationSender()
    await sender.send(
        NotificationMessage(recipient_user_id=_UPLOADER, subject="s", body="b", channel="log")
    )
    assert sender.sent[0].subject == "s"
