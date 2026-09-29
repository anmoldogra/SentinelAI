"""Unit tests for the notification module's operator surface — api-design.md §4.9/§8.

Rule CRUD, the redelivery endpoint, and the two things that surface belongs to: the ETag guard that
stops one admin silently overwriting another, and the audit entry that makes a change to *who gets
told* visible afterwards.

The dispatch path has its own suites (`test_notification_consumers.py`,
`test_notification_scan_consumer.py`); the whole thing against real Postgres is
`test_notification_dispatch_db.py`.

The test this file exists for is `test_an_audited_method_without_a_kms_fails_rather_than_skipping`.
security-architecture §22 requires that there be "no alternate route that produces an unaudited side
effect", and this service is deliberately constructible without a KMS — because the event dispatcher
builds it that way. Failing closed is what keeps that constructor from being such a route.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinelai.modules.notification import service as service_module
from sentinelai.modules.notification.exceptions import (
    NothingToRedeliverError,
    NotificationNotFoundError,
    NotificationRuleNotFoundError,
)
from sentinelai.modules.notification.models import (
    Notification,
    NotificationDelivery,
    NotificationRule,
)
from sentinelai.modules.notification.schemas import NotificationRuleCreate, NotificationRuleUpdate
from sentinelai.modules.notification.service import (
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    TRIGGER_EVENT_TYPES,
    NotificationService,
    rule_etag,
)
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.notifications import NotificationMessage
from sentinelai.shared.exceptions import (
    ForbiddenError,
    PreconditionFailedError,
    ValidationFailedError,
)
from sentinelai.shared.pagination import PageParams

_ADMIN = CurrentUser(user_id=uuid4(), roles=("admin",))
_RECIPIENT = uuid4()
_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class _FakeRuleRepo:
    def __init__(self) -> None:
        self.items: list[NotificationRule] = []

    async def get_by_id(self, rule_id: UUID) -> NotificationRule | None:
        return next((r for r in self.items if r.rule_id == rule_id), None)

    async def add(self, rule: NotificationRule) -> None:
        if rule.rule_id is None:
            rule.rule_id = uuid4()
        self.items.append(rule)

    async def list_(self) -> list[NotificationRule]:
        return sorted(self.items, key=lambda r: (r.name, str(r.rule_id)))


class _FakeNotificationRepo:
    def __init__(self) -> None:
        self.items: list[Notification] = []

    async def get_by_id(self, notification_id: UUID) -> Notification | None:
        return next((n for n in self.items if n.notification_id == notification_id), None)

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

    async def list_for_recipient(
        self,
        recipient_user_id: UUID,
        *,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_notification_id: UUID | None,
        read: bool | None = None,
    ) -> list[Notification]:
        rows = [n for n in self.items if n.recipient_user_id == recipient_user_id]
        if read is True:
            rows = [n for n in rows if n.read_at is not None]
        elif read is False:
            rows = [n for n in rows if n.read_at is None]
        rows.sort(key=lambda n: (n.created_at, n.notification_id), reverse=True)
        return rows[: limit + 1]


class _FakeDeliveryRepo:
    def __init__(self) -> None:
        self.items: list[NotificationDelivery] = []

    async def add(self, delivery: NotificationDelivery) -> None:
        if delivery.delivery_id is None:
            delivery.delivery_id = uuid4()
        self.items.append(delivery)

    async def list_for_notification(self, notification_id: UUID) -> list[NotificationDelivery]:
        rows = [d for d in self.items if d.notification_id == notification_id]
        # Newest attempt first, matching the real query's ordering — a fake that returned them
        # chronologically would let `redeliver` read the wrong attempt and still pass.
        rows.sort(key=lambda d: (d.attempted_at or _NOW, str(d.delivery_id)), reverse=True)
        return rows


class _FakeOutbox:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


class _FakeUow:
    def __init__(self) -> None:
        self.session = object()
        self.rules = _FakeRuleRepo()
        self.notifications = _FakeNotificationRepo()
        self.deliveries = _FakeDeliveryRepo()
        self.outbox = _FakeOutbox()
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _RecordingSender:
    channel = "log"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[NotificationMessage] = []
        self._fail = fail

    async def send(self, message: NotificationMessage) -> None:
        if self._fail:
            raise RuntimeError("channel unavailable")
        self.sent.append(message)


@pytest.fixture
def uow() -> _FakeUow:
    return _FakeUow()


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture audit writes. `record_audit_event` needs a real session, so it is replaced here."""
    captured: list[dict[str, Any]] = []

    async def _capture(_session: Any, **kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(service_module, "record_audit_event", _capture)
    return captured


# A stand-in KMS: `_audit` is monkeypatched in these tests, so the object is never used — it only
# has
# to be present, which is exactly the condition `_audit` enforces.
_KMS_PRESENT: Any = object()


def _service(uow: _FakeUow, *, sender: Any = None, kms: Any = _KMS_PRESENT) -> NotificationService:
    return NotificationService(uow, sender=sender or _RecordingSender(), kms=kms)


def _rule(**overrides: Any) -> NotificationRule:
    fields: dict[str, Any] = {
        "rule_id": uuid4(),
        "name": "Alert supervisor on new findings",
        "trigger_event_type": "investigation.correlation_generated",
        "channel": "log",
        "target_role_or_user": uuid4(),
        "is_active": True,
    }
    fields.update(overrides)
    return NotificationRule(**fields)


def _notification(**overrides: Any) -> Notification:
    fields: dict[str, Any] = {
        "notification_id": uuid4(),
        "rule_id": None,
        "recipient_user_id": _RECIPIENT,
        "source_module": "investigation",
        "source_reference_id": uuid4(),
        "message": "A new relationship was proposed for your case.",
        "created_at": _NOW,
        "read_at": None,
    }
    fields.update(overrides)
    return Notification(**fields)


# --- the trigger vocabulary -------------------------------------------------
def test_the_trigger_vocabulary_is_25s_published_catalog() -> None:
    """One entry per row of §25.1-§25.9's Published tables, and nothing else.

    Spot-checked rather than re-listed: the count pins the size, and the three below are the ones a
    wrong source would get wrong. `evidence.scanned` and `case.report_generated` are **absent from
    `system-design.md` §6**, which api-design.md §8 points at — so their presence here is the
    assertion that §25 was used instead, and they are two of the four events this module consumes.
    """
    assert len(TRIGGER_EVENT_TYPES) == 28
    assert "evidence.scanned" in TRIGGER_EVENT_TYPES
    assert "case.report_generated" in TRIGGER_EVENT_TYPES
    assert "investigation.correlation_run_failed" in TRIGGER_EVENT_TYPES


def test_every_event_this_module_consumes_is_a_valid_trigger() -> None:
    """The two lists that must not drift apart.

    A rule cannot be configured for an event this module already reacts to unless that event is in
    the vocabulary — and a mismatch would be an admin told their own platform's events are invalid.
    """
    from sentinelai.modules.notification.events import (
        EVENT_CASE_REPORT_GENERATED,
        EVENT_CASE_STATUS_CHANGED,
        EVENT_CORRELATION_GENERATED,
        EVENT_EVIDENCE_SCANNED,
    )

    for event_type in (
        EVENT_CORRELATION_GENERATED,
        EVENT_CASE_STATUS_CHANGED,
        EVENT_CASE_REPORT_GENERATED,
        EVENT_EVIDENCE_SCANNED,
    ):
        assert event_type in TRIGGER_EVENT_TYPES


# --- create_rule ------------------------------------------------------------
async def test_a_rule_is_created_active_and_audited(uow: _FakeUow, audit: list[Any]) -> None:
    """§7 gives config data an `is_active` lifecycle rather than deletion, so a new rule is active —
    one created inactive would be configuration that silently does nothing."""
    target = uuid4()

    rule = await _service(uow).create_rule(
        NotificationRuleCreate(
            name="Alert on findings",
            trigger_event_type="investigation.correlation_generated",
            channel="log",
            target_role_or_user=target,
        ),
        _ADMIN,
        "corr-1",
    )

    assert rule.is_active is True
    assert uow.rules.items == [rule]
    assert uow.commits == 0  # ADR-0005: the entrypoint commits, not the service
    [entry] = audit
    assert entry["action"] == "notification_rule.created"
    # The field that decides who gets told is in the entry, which is the point of auditing this.
    assert entry["details"]["target_role_or_user"] == str(target)


async def test_a_rule_on_an_unpublished_event_is_refused(uow: _FakeUow, audit: list[Any]) -> None:
    """**The failure this validation exists to prevent is silence.** A rule naming an event nobody
    publishes never fires, and the admin who configured the alert would discover that only from the
    alert never arriving."""
    with pytest.raises(ValidationFailedError) as caught:
        await _service(uow).create_rule(
            NotificationRuleCreate(
                name="Alert on nothing",
                trigger_event_type="investigation.hypothesis_generated",
                channel="log",
                target_role_or_user=uuid4(),
            ),
            _ADMIN,
            "corr-1",
        )

    assert any(d["field"] == "trigger_event_type" for d in caught.value.details)
    assert uow.rules.items == []
    assert audit == []  # nothing happened, so nothing is recorded


async def test_a_rule_publishes_no_event(uow: _FakeUow, audit: list[Any]) -> None:
    """§25.9's published table holds two events, both about deliveries. There is no rule-lifecycle
    event, and inventing one would be inventing catalog."""
    await _service(uow).create_rule(
        NotificationRuleCreate(
            name="Alert on findings",
            trigger_event_type="case.status_changed",
            channel="log",
            target_role_or_user=uuid4(),
        ),
        _ADMIN,
        "corr-1",
    )

    assert uow.outbox.published == []


# --- list_rules -------------------------------------------------------------
async def test_listing_includes_deactivated_rules(uow: _FakeUow) -> None:
    """§7's lifecycle for config data is a flag, not deletion: a deactivated rule is still
    configuration an admin must see and be able to re-enable. Hiding it would make the flag look
    like a delete."""
    active = _rule(name="A active")
    inactive = _rule(name="B inactive", is_active=False)
    uow.rules.items.extend([active, inactive])

    rules = await _service(uow).list_rules(_ADMIN)

    assert [r.name for r in rules] == ["A active", "B inactive"]


# --- update_rule ------------------------------------------------------------
async def test_a_rule_is_updated_under_a_matching_etag(uow: _FakeUow, audit: list[Any]) -> None:
    rule = _rule()
    uow.rules.items.append(rule)

    updated = await _service(uow).update_rule(
        rule.rule_id,
        NotificationRuleUpdate(name="Renamed", is_active=False),
        _ADMIN,
        rule_etag(rule),
    )

    assert (updated.name, updated.is_active) == ("Renamed", False)
    [entry] = audit
    assert entry["action"] == "notification_rule.updated"
    assert entry["details"]["changes"]["is_active"] == {"from": True, "to": False}


async def test_a_stale_etag_is_refused_before_anything_mutates(
    uow: _FakeUow, audit: list[Any]
) -> None:
    """**Two admins editing one rule must not have one silently overwrite the other** — for a rule
    that means one of them unknowingly re-enabling an alert the other just turned off."""
    rule = _rule()
    uow.rules.items.append(rule)

    with pytest.raises(PreconditionFailedError):
        await _service(uow).update_rule(
            rule.rule_id, NotificationRuleUpdate(is_active=False), _ADMIN, 'W/"stale"'
        )

    assert rule.is_active is True
    assert audit == []


async def test_the_etag_changes_only_with_a_mutable_field(uow: _FakeUow, audit: list[Any]) -> None:
    """Including an immutable field would make the ETag churn on nothing; including only the id
    would make it never change and the `If-Match` guard decorative."""
    rule = _rule()
    uow.rules.items.append(rule)
    before = rule_etag(rule)

    await _service(uow).update_rule(
        rule.rule_id, NotificationRuleUpdate(channel="email"), _ADMIN, before
    )

    assert rule_etag(rule) != before
    # And the same rule read twice with no change gives the same value.
    assert rule_etag(rule) == rule_etag(rule)


async def test_an_update_of_a_missing_rule_is_a_404(uow: _FakeUow, audit: list[Any]) -> None:
    with pytest.raises(NotificationRuleNotFoundError):
        await _service(uow).update_rule(
            uuid4(), NotificationRuleUpdate(name="x"), _ADMIN, 'W/"whatever"'
        )


async def test_a_no_op_update_is_still_audited(uow: _FakeUow, audit: list[Any]) -> None:
    """An admin who sent a PATCH with a valid `If-Match` acted on this rule. An oversight review
    asking "who touched the alerting configuration" wants that, not only the non-empty diffs."""
    rule = _rule()
    uow.rules.items.append(rule)

    await _service(uow).update_rule(
        rule.rule_id, NotificationRuleUpdate(name=rule.name), _ADMIN, rule_etag(rule)
    )

    [entry] = audit
    assert entry["details"]["changes"] == {}


# --- the audit invariant ----------------------------------------------------
async def test_an_audited_method_without_a_kms_fails_rather_than_skipping(uow: _FakeUow) -> None:
    """**The test this file exists for.**

    security-architecture §22: "there is exactly one path to write an audit entry, and it is not
    optional or skippable by any code path that performs an audited action ... no alternate route
    that produces an unaudited side effect". This service is constructible without a KMS on
    purpose —
    the event dispatcher builds it that way, and the methods it reaches do not audit. Failing closed
    is what stops that constructor being an unaudited route into the ones that do.
    """
    service = NotificationService(uow, sender=_RecordingSender(), kms=None)

    with pytest.raises(RuntimeError, match="requires a KMS"):
        await service.create_rule(
            NotificationRuleCreate(
                name="Sneaky",
                trigger_event_type="case.status_changed",
                channel="log",
                target_role_or_user=uuid4(),
            ),
            _ADMIN,
            "corr-1",
        )


async def test_the_dispatch_path_needs_no_kms(uow: _FakeUow) -> None:
    """The other half: the event dispatcher hands a handler a session and a signed outbox and
    nothing else, and the dispatch methods must work with exactly that. §8 gives them no audit
    requirement — they are the platform reacting to its own observation, not an operator acting."""
    service = NotificationService(uow, sender=_RecordingSender(), kms=None)

    notification = await service.dispatch_for_correlation_generated(
        case_id=uuid4(),
        relationship_id=uuid4(),
        recipient_user_id=_RECIPIENT,
        correlation_id="corr-1",
    )

    assert notification is not None
    assert len(uow.deliveries.items) == 1


# --- redeliver --------------------------------------------------------------
async def test_a_failed_delivery_is_retried_and_audited(uow: _FakeUow, audit: list[Any]) -> None:
    notification = _notification()
    uow.notifications.items.append(notification)
    uow.deliveries.items.append(
        NotificationDelivery(
            delivery_id=uuid4(),
            notification_id=notification.notification_id,
            channel="log",
            delivery_status=DELIVERY_FAILED,
            attempted_at=_NOW,
            delivered_at=None,
        )
    )
    sender = _RecordingSender()

    delivery = await _service(uow, sender=sender).redeliver(
        notification.notification_id, _ADMIN, "corr-1"
    )

    assert delivery.delivery_status == DELIVERY_DELIVERED
    assert len(sender.sent) == 1
    # A retry adds an attempt; it never adds a notification.
    assert len(uow.deliveries.items) == 2
    assert len(uow.notifications.items) == 1
    [entry] = audit
    assert entry["action"] == "notification.redelivered"
    assert entry["details"]["previous_attempts"] == 1


async def test_redelivering_a_delivered_notification_is_refused(
    uow: _FakeUow, audit: list[Any]
) -> None:
    """**This is what keeps the endpoint from being a way around §25.9.** §4.9 defines it as
    retrying a *failed* delivery, and re-sending a delivered one is exactly the duplicate the
    catalog's tightest key exists to prevent."""
    notification = _notification()
    uow.notifications.items.append(notification)
    uow.deliveries.items.append(
        NotificationDelivery(
            delivery_id=uuid4(),
            notification_id=notification.notification_id,
            channel="log",
            delivery_status=DELIVERY_DELIVERED,
            attempted_at=_NOW,
            delivered_at=_NOW,
        )
    )
    sender = _RecordingSender()

    with pytest.raises(NothingToRedeliverError):
        await _service(uow, sender=sender).redeliver(notification.notification_id, _ADMIN, "corr-1")

    assert sender.sent == []
    assert len(uow.deliveries.items) == 1
    assert audit == []


async def test_a_notification_with_no_attempts_is_redeliverable(
    uow: _FakeUow, audit: list[Any]
) -> None:
    """What a delivery row lost to a rolled-back transaction looks like. An analyst holding an
    undelivered notification is the case this endpoint is for."""
    notification = _notification()
    uow.notifications.items.append(notification)

    delivery = await _service(uow).redeliver(notification.notification_id, _ADMIN, "corr-1")

    assert delivery.delivery_status == DELIVERY_DELIVERED
    assert audit[0]["details"]["previous_attempts"] == 0


async def test_redelivering_a_missing_notification_is_a_404(uow: _FakeUow) -> None:
    with pytest.raises(NotificationNotFoundError):
        await _service(uow).redeliver(uuid4(), _ADMIN, "corr-1")


async def test_a_failed_retry_is_recorded_not_raised(uow: _FakeUow, audit: list[Any]) -> None:
    """A channel failure must not discard the notification: the in-app row is the durable delivery,
    so the outcome is recorded and reported. Raising would roll back the admin's whole request."""
    notification = _notification()
    uow.notifications.items.append(notification)

    delivery = await _service(uow, sender=_RecordingSender(fail=True)).redeliver(
        notification.notification_id, _ADMIN, "corr-1"
    )

    assert delivery.delivery_status == DELIVERY_FAILED
    assert uow.outbox.published[0]["event_type"] == "notification.delivery_failed"
    assert audit[0]["details"]["delivery_status"] == DELIVERY_FAILED


async def test_a_retry_resends_the_stored_message(uow: _FakeUow, audit: list[Any]) -> None:
    """The analyst gets what they should have got the first time, not a re-derived message: the row
    is the record of what was meant to be sent."""
    notification = _notification(message="Original wording, preserved.")
    uow.notifications.items.append(notification)
    sender = _RecordingSender()

    await _service(uow, sender=sender).redeliver(notification.notification_id, _ADMIN, "corr-1")

    assert sender.sent[0].body == "Original wording, preserved."


# --- the inbox's documented filter ------------------------------------------
@pytest.mark.parametrize(
    ("read", "expected"),
    [(None, 2), (True, 1), (False, 1)],
    ids=["unfiltered", "read-only", "unread-only"],
)
async def test_the_read_filter_selects_on_read_at(
    uow: _FakeUow, read: bool | None, expected: int
) -> None:
    """§8 documents `read` as a boolean filter. It is expressed against `read_at` because that
    column *is* the read state — there is no separate flag that could drift from it."""
    uow.notifications.items.extend(
        [
            _notification(read_at=None, created_at=_NOW),
            _notification(read_at=_NOW, created_at=_NOW),
        ]
    )
    actor = CurrentUser(user_id=_RECIPIENT, roles=("investigator",))

    items, _cursor, _has_more = await _service(uow).list_notifications(
        actor, PageParams(cursor=None, limit=50), read=read
    )

    assert len(items) == expected


async def test_marking_someone_elses_notification_read_is_forbidden(uow: _FakeUow) -> None:
    """§8's deliberate exception to the NOT_FOUND-hides-existence convention: 403, not 404."""
    uow.notifications.items.append(_notification())
    stranger = CurrentUser(user_id=uuid4(), roles=("investigator",))

    with pytest.raises(ForbiddenError):
        await _service(uow).mark_read(uow.notifications.items[0].notification_id, stranger)
