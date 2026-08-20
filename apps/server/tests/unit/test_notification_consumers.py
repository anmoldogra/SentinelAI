"""Unit tests for the notification module's three remaining event consumers.

`investigation.correlation_generated`, `case.status_changed`, and `case.report_generated` —
each driven through its real handler and the real service against in-memory fakes, covering the
two-layer idempotency the §25.9 catalog specifies: the Inbox claim (same event redelivered) and
the per-handler business key (a different event describing the same fact).

The `evidence.scanned` consumer has its own suite in ``test_notification_scan_consumer.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinelai.modules.notification import events as notification_events
from sentinelai.modules.notification.events import (
    on_case_report_generated,
    on_case_status_changed,
    on_correlation_generated,
)
from sentinelai.modules.notification.models import Notification
from sentinelai.platform.events.envelope import EventEnvelope

_CASE_ID = uuid4()
_RELATIONSHIP_ID = uuid4()
_REPORT_ID = uuid4()
_INVESTIGATOR = uuid4()


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
    def __init__(self) -> None:
        self.claims: set[tuple[UUID, str]] = set()
        self.processed: set[tuple[UUID, str]] = set()


class _FakeUow:
    def __init__(self, inbox: _FakeInbox) -> None:
        self.session = inbox  # the fake guard reads its claim set from "the session"
        self.notifications = _FakeNotificationRepo()
        self.deliveries = _FakeDeliveryRepo()
        self.outbox = _FakeOutbox()
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def inbox() -> _FakeInbox:
    return _FakeInbox()


@pytest.fixture(autouse=True)
def _fake_inbox_guard(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.fixture(autouse=True)
def _null_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the service off the config-driven sender factory."""

    class _NullSender:
        channel = "log"

        async def send(self, message: Any) -> None:
            return None

    monkeypatch.setattr(
        "sentinelai.modules.notification.service.build_notification_sender",
        lambda cfg=None: _NullSender(),
    )


def _event(event_type: str, payload: dict[str, Any]) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        event_version="1.0.0",
        aggregate_type="x",
        aggregate_id=uuid4(),
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


def _correlation_event(**overrides: Any) -> EventEnvelope:
    payload: dict[str, Any] = {
        "case_id": str(_CASE_ID),
        "relationship_id": str(_RELATIONSHIP_ID),
        "confidence": "0.82",
        "recipient_user_id": str(_INVESTIGATOR),
    }
    payload.update(overrides)
    return _event("investigation.correlation_generated", payload)


def _status_event(**overrides: Any) -> EventEnvelope:
    payload: dict[str, Any] = {
        "case_id": str(_CASE_ID),
        "previous_status": "open",
        "new_status": "closed",
        "owning_user_id": str(_INVESTIGATOR),
    }
    payload.update(overrides)
    return _event("case.status_changed", payload)


def _report_event(**overrides: Any) -> EventEnvelope:
    payload: dict[str, Any] = {
        "case_id": str(_CASE_ID),
        "report_id": str(_REPORT_ID),
        "requested_by_user_id": str(_INVESTIGATOR),
    }
    payload.update(overrides)
    return _event("case.report_generated", payload)


# --- investigation.correlation_generated ------------------------------------


async def test_a_proposed_finding_notifies_the_case_investigator(inbox: _FakeInbox) -> None:
    uow = _FakeUow(inbox)
    await on_correlation_generated(_correlation_event(), uow)

    assert len(uow.notifications.items) == 1
    notification = uow.notifications.items[0]
    assert notification.recipient_user_id == _INVESTIGATOR
    assert notification.source_module == "investigation"
    assert notification.source_reference_id == _RELATIONSHIP_ID  # keyed on the relationship


async def test_the_finding_message_asks_for_review_not_approval(inbox: _FakeInbox) -> None:
    """PRD FR-7.3: the notification prompts human review; it never implies acceptance."""
    uow = _FakeUow(inbox)
    await on_correlation_generated(_correlation_event(), uow)
    message = uow.notifications.items[0].message or ""
    assert "awaiting your review" in message
    assert "remains proposed" in message


async def test_a_replayed_finding_event_notifies_only_once(inbox: _FakeInbox) -> None:
    """§25.9 calls this the tightest key in the catalog."""
    uow = _FakeUow(inbox)
    await on_correlation_generated(_correlation_event(), uow)
    await on_correlation_generated(_correlation_event(), uow)  # new event_id, same relationship
    assert len(uow.notifications.items) == 1


async def test_a_second_finding_on_the_same_case_does_notify(inbox: _FakeInbox) -> None:
    uow = _FakeUow(inbox)
    await on_correlation_generated(_correlation_event(), uow)
    await on_correlation_generated(_correlation_event(relationship_id=str(uuid4())), uow)
    assert len(uow.notifications.items) == 2


async def test_a_finding_event_without_a_recipient_is_ignored(inbox: _FakeInbox) -> None:
    """The correlation job may omit the owner; ignore rather than dead-letter a valid fact."""
    uow = _FakeUow(inbox)
    event = _correlation_event(recipient_user_id=None)
    await on_correlation_generated(event, uow)
    assert uow.notifications.items == []
    assert (event.event_id, "notification.on_correlation_generated") in inbox.processed


# --- case.status_changed -----------------------------------------------------


async def test_a_status_transition_notifies_the_case_owner(inbox: _FakeInbox) -> None:
    uow = _FakeUow(inbox)
    await on_case_status_changed(_status_event(), uow)

    notification = uow.notifications.items[0]
    assert notification.recipient_user_id == _INVESTIGATOR
    assert notification.source_module == "case_management"
    assert notification.source_reference_id == _CASE_ID
    assert "closed" in (notification.message or "")


async def test_reaching_the_same_status_twice_notifies_once(inbox: _FakeInbox) -> None:
    """Key is (recipient, case_id, new_status) — the status is the discriminator."""
    uow = _FakeUow(inbox)
    await on_case_status_changed(_status_event(), uow)
    await on_case_status_changed(_status_event(), uow)
    assert len(uow.notifications.items) == 1


async def test_a_different_status_on_the_same_case_does_notify(inbox: _FakeInbox) -> None:
    """The status discriminator must not collapse distinct transitions into one."""
    uow = _FakeUow(inbox)
    await on_case_status_changed(_status_event(new_status="closed"), uow)
    await on_case_status_changed(_status_event(new_status="archived"), uow)
    assert len(uow.notifications.items) == 2


async def test_the_status_message_does_not_vary_with_the_previous_status(
    inbox: _FakeInbox,
) -> None:
    """The stored message carries the key, so it must depend only on (case_id, new_status)."""
    uow = _FakeUow(inbox)
    await on_case_status_changed(_status_event(previous_status="open", new_status="closed"), uow)
    first = uow.notifications.items[0].message
    await on_case_status_changed(
        _status_event(previous_status="archived", new_status="closed"), uow
    )
    assert len(uow.notifications.items) == 1  # deduped despite a different previous_status
    assert uow.notifications.items[0].message == first


async def test_a_status_event_without_an_owner_is_ignored(inbox: _FakeInbox) -> None:
    uow = _FakeUow(inbox)
    await on_case_status_changed(_status_event(owning_user_id=None), uow)
    assert uow.notifications.items == []


# --- case.report_generated ---------------------------------------------------


async def test_a_finished_report_notifies_the_requester(inbox: _FakeInbox) -> None:
    uow = _FakeUow(inbox)
    await on_case_report_generated(_report_event(), uow)

    notification = uow.notifications.items[0]
    assert notification.recipient_user_id == _INVESTIGATOR
    assert notification.source_reference_id == _REPORT_ID  # keyed on the report, not the case
    assert "ready to download" in (notification.message or "")


async def test_a_replayed_report_event_notifies_only_once(inbox: _FakeInbox) -> None:
    uow = _FakeUow(inbox)
    await on_case_report_generated(_report_event(), uow)
    await on_case_report_generated(_report_event(), uow)
    assert len(uow.notifications.items) == 1


async def test_a_regenerated_report_is_a_new_fact_and_does_notify(inbox: _FakeInbox) -> None:
    uow = _FakeUow(inbox)
    await on_case_report_generated(_report_event(), uow)
    await on_case_report_generated(_report_event(report_id=str(uuid4())), uow)
    assert len(uow.notifications.items) == 2


async def test_the_report_recipient_falls_back_to_the_case_owner(inbox: _FakeInbox) -> None:
    """Until the deferred job supplies requested_by_user_id, an owning_user_id still works."""
    uow = _FakeUow(inbox)
    event = _report_event(requested_by_user_id=None, owning_user_id=str(_INVESTIGATOR))
    await on_case_report_generated(event, uow)
    assert uow.notifications.items[0].recipient_user_id == _INVESTIGATOR


# --- shared consumer invariants ----------------------------------------------


@pytest.mark.parametrize(
    ("handler", "make_event", "handler_name"),
    [
        (on_correlation_generated, _correlation_event, "notification.on_correlation_generated"),
        (on_case_status_changed, _status_event, "notification.on_case_status_changed"),
        (on_case_report_generated, _report_event, "notification.on_case_report_generated"),
    ],
)
async def test_redelivering_the_same_event_short_circuits_on_the_inbox_claim(
    inbox: _FakeInbox, handler: Any, make_event: Any, handler_name: str
) -> None:
    uow = _FakeUow(inbox)
    event = make_event()
    await handler(event, uow)
    await handler(event, uow)  # identical event_id
    assert len(uow.notifications.items) == 1
    assert len(uow.outbox.published) == 1
    assert (event.event_id, handler_name) in inbox.processed


@pytest.mark.parametrize(
    ("handler", "make_event"),
    [
        (on_correlation_generated, _correlation_event),
        (on_case_status_changed, _status_event),
        (on_case_report_generated, _report_event),
    ],
)
async def test_each_handler_records_a_delivery_and_publishes_the_outcome(
    inbox: _FakeInbox, handler: Any, make_event: Any
) -> None:
    uow = _FakeUow(inbox)
    await handler(make_event(), uow)
    assert uow.deliveries.items[0].delivery_status == "delivered"
    assert [e["event_type"] for e in uow.outbox.published] == ["notification.dispatched"]


@pytest.mark.parametrize(
    ("handler", "make_event"),
    [
        (on_correlation_generated, _correlation_event),
        (on_case_status_changed, _status_event),
        (on_case_report_generated, _report_event),
    ],
)
async def test_no_handler_commits(inbox: _FakeInbox, handler: Any, make_event: Any) -> None:
    """ADR-0005: the dispatcher owns the handler's transaction."""
    uow = _FakeUow(inbox)
    await handler(make_event(), uow)
    assert uow.commits == 0
