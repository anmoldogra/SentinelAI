"""notification event wiring — event-driven-architecture.md §25.9.

Terminal consumer: reacts to upstream facts by creating + dispatching notifications.
Published: delivery-outcome facts (ops/metrics use). Every handler performs the
Inbox claim before any side effect; the service enforces the catalog's per-handler
business-idempotency key so a replay never re-sends a delivered message.
"""

from __future__ import annotations

from uuid import UUID

from sentinelai.modules.notification.repository import NotificationUnitOfWork
from sentinelai.platform.events.dispatcher import EventDispatcher, RetryPolicy
from sentinelai.platform.events.envelope import EventEnvelope
from sentinelai.platform.events.inbox import InboxGuard

SCHEMA = "notification"

# Published (§25.9).
EVENT_NOTIFICATION_DISPATCHED = "notification.dispatched"
EVENT_NOTIFICATION_DELIVERY_FAILED = "notification.delivery_failed"

# Consumed (§25.9) — all "Critical-fast".
EVENT_CORRELATION_GENERATED = "investigation.correlation_generated"
EVENT_CASE_STATUS_CHANGED = "case.status_changed"
EVENT_CASE_REPORT_GENERATED = "case.report_generated"
EVENT_EVIDENCE_SCANNED = "evidence.scanned"

_CRITICAL_FAST = RetryPolicy(max_attempts=10)
_HANDLER_EVIDENCE_SCANNED = "notification.on_evidence_scanned"
_HANDLER_CORRELATION_GENERATED = "notification.on_correlation_generated"
_HANDLER_CASE_STATUS_CHANGED = "notification.on_case_status_changed"
_HANDLER_CASE_REPORT_GENERATED = "notification.on_case_report_generated"


async def on_evidence_scanned(event: EventEnvelope, uow: NotificationUnitOfWork) -> None:
    """Notify the uploading analyst when a malware detection BLOCKED their upload (§25).

    Consumes every ``evidence.scanned`` outcome and acts on one: a detection that was not
    promoted. A clean scan is normal, and a forensic-category detection is promoted on purpose
    (§25's carve-out — malware in a disk image is the evidence), so neither notifies.

    Never commits: the dispatcher owns this handler's transaction (ADR-0005), so the inbox claim,
    the notification row, the delivery row, and the outbox event all land atomically or not at all.
    """
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_HANDLER_EVIDENCE_SCANNED):
        return  # redelivery — the side effect already ran

    payload = event.payload
    blocked = not bool(payload.get("is_clean", True)) and not bool(payload.get("promoted", True))
    recipient = payload.get("collector_user_id")
    if blocked and recipient:
        # Imported here, not at module scope: service.py imports this module's published-event
        # constants, so a top-level import would close an events <-> service cycle.
        from sentinelai.modules.notification.service import NotificationService

        await NotificationService(uow).dispatch_for_evidence_scanned(
            evidence_id=UUID(str(payload["evidence_id"])),
            recipient_user_id=UUID(str(recipient)),
            detection_name=(
                str(payload["detection_name"]) if payload.get("detection_name") else None
            ),
            engine=str(payload.get("engine", "unknown")),
            correlation_id=str(event.correlation_id),
        )

    await guard.mark_processed(event.event_id, handler_name=_HANDLER_EVIDENCE_SCANNED)


def _recipient(payload: dict[str, object], *keys: str) -> UUID | None:
    """First present recipient id among ``keys``, or ``None``.

    An event whose payload names no recipient is consumed and ignored rather than failing: the
    handler cannot invent someone to notify, and dead-lettering an otherwise valid upstream fact
    would be worse than not sending a message.
    """
    for key in keys:
        value = payload.get(key)
        if value:
            return UUID(str(value))
    return None


async def on_correlation_generated(event: EventEnvelope, uow: NotificationUnitOfWork) -> None:
    """Create + dispatch a notification to the case's assigned investigator(s)."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_HANDLER_CORRELATION_GENERATED):
        return

    payload = event.payload
    recipient = _recipient(payload, "recipient_user_id")
    relationship_id = payload.get("relationship_id")
    if recipient and relationship_id:
        from sentinelai.modules.notification.service import NotificationService

        await NotificationService(uow).dispatch_for_correlation_generated(
            case_id=UUID(str(payload["case_id"])),
            relationship_id=UUID(str(relationship_id)),
            recipient_user_id=recipient,
            correlation_id=str(event.correlation_id),
        )

    await guard.mark_processed(event.event_id, handler_name=_HANDLER_CORRELATION_GENERATED)


async def on_case_status_changed(event: EventEnvelope, uow: NotificationUnitOfWork) -> None:
    """Notify assigned investigator(s) of a case status transition."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_HANDLER_CASE_STATUS_CHANGED):
        return

    payload = event.payload
    recipient = _recipient(payload, "owning_user_id")
    if recipient:
        from sentinelai.modules.notification.service import NotificationService

        await NotificationService(uow).dispatch_for_case_status_changed(
            case_id=UUID(str(payload["case_id"])),
            new_status=str(payload["new_status"]),
            recipient_user_id=recipient,
            correlation_id=str(event.correlation_id),
        )

    await guard.mark_processed(event.event_id, handler_name=_HANDLER_CASE_STATUS_CHANGED)


async def on_case_report_generated(event: EventEnvelope, uow: NotificationUnitOfWork) -> None:
    """Notify the requester that a report is ready for download."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name=_HANDLER_CASE_REPORT_GENERATED):
        return

    payload = event.payload
    recipient = _recipient(payload, "requested_by_user_id", "owning_user_id")
    report_id = payload.get("report_id")
    if recipient and report_id:
        from sentinelai.modules.notification.service import NotificationService

        await NotificationService(uow).dispatch_for_report_generated(
            case_id=UUID(str(payload["case_id"])),
            report_id=UUID(str(report_id)),
            recipient_user_id=recipient,
            correlation_id=str(event.correlation_id),
        )

    await guard.mark_processed(event.event_id, handler_name=_HANDLER_CASE_REPORT_GENERATED)


def register_consumers(dispatcher: EventDispatcher) -> None:
    dispatcher.register(
        EVENT_EVIDENCE_SCANNED,
        on_evidence_scanned,
        inbox_schema=SCHEMA,
        uow_factory=NotificationUnitOfWork,
        policy=_CRITICAL_FAST,
    )
    dispatcher.register(
        EVENT_CORRELATION_GENERATED,
        on_correlation_generated,
        inbox_schema=SCHEMA,
        uow_factory=NotificationUnitOfWork,
        policy=_CRITICAL_FAST,
    )
    dispatcher.register(
        EVENT_CASE_STATUS_CHANGED,
        on_case_status_changed,
        inbox_schema=SCHEMA,
        uow_factory=NotificationUnitOfWork,
        policy=_CRITICAL_FAST,
    )
    dispatcher.register(
        EVENT_CASE_REPORT_GENERATED,
        on_case_report_generated,
        inbox_schema=SCHEMA,
        uow_factory=NotificationUnitOfWork,
        policy=_CRITICAL_FAST,
    )
