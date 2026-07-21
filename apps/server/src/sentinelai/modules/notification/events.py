"""notification event wiring — event-driven-architecture.md §25.9.

Terminal consumer: reacts to upstream facts by creating + dispatching notifications.
Published: delivery-outcome facts (ops/metrics use). Every handler performs the
Inbox claim before any side effect; the service enforces the catalog's per-handler
business-idempotency key so a replay never re-sends a delivered message.
"""

from __future__ import annotations

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

_CRITICAL_FAST = RetryPolicy(max_attempts=10)


async def on_correlation_generated(event: EventEnvelope, uow: NotificationUnitOfWork) -> None:
    """Create + dispatch a notification to the case's assigned investigator(s)."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name="notification.on_correlation_generated"):
        return
    raise NotImplementedError


async def on_case_status_changed(event: EventEnvelope, uow: NotificationUnitOfWork) -> None:
    """Notify assigned investigator(s) of a case status transition."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name="notification.on_case_status_changed"):
        return
    raise NotImplementedError


async def on_case_report_generated(event: EventEnvelope, uow: NotificationUnitOfWork) -> None:
    """Notify the requester that a report is ready for download."""
    guard = InboxGuard(uow.session, schema=SCHEMA)
    if not await guard.try_claim(event.event_id, handler_name="notification.on_case_report_generated"):
        return
    raise NotImplementedError


def register_consumers(dispatcher: EventDispatcher) -> None:
    dispatcher.register(
        EVENT_CORRELATION_GENERATED, on_correlation_generated,
        inbox_schema=SCHEMA, uow_factory=NotificationUnitOfWork, policy=_CRITICAL_FAST,
    )
    dispatcher.register(
        EVENT_CASE_STATUS_CHANGED, on_case_status_changed,
        inbox_schema=SCHEMA, uow_factory=NotificationUnitOfWork, policy=_CRITICAL_FAST,
    )
    dispatcher.register(
        EVENT_CASE_REPORT_GENERATED, on_case_report_generated,
        inbox_schema=SCHEMA, uow_factory=NotificationUnitOfWork, policy=_CRITICAL_FAST,
    )
