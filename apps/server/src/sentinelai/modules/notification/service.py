"""notification business logic (guide Part 5) — rule management, the caller's
notification inbox, and dispatch driven by consumed events.

``dispatch_for_evidence_scanned`` is implemented (security-architecture §25's "notify the
uploading analyst" on a malware block); the rule-management and notification-inbox reads that
serve ``router.py`` are still deferred (``NotImplementedError``).

Dispatch handlers enforce the catalog's tight business-idempotency keys (§25.9) so
a replayed event never re-sends a message the analyst already received.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.notification.events import (
    EVENT_NOTIFICATION_DELIVERY_FAILED,
    EVENT_NOTIFICATION_DISPATCHED,
)
from sentinelai.modules.notification.exceptions import NotificationNotFoundError
from sentinelai.modules.notification.models import (
    Notification,
    NotificationDelivery,
    NotificationRule,
)
from sentinelai.modules.notification.repository import NotificationUnitOfWork, get_notification_uow
from sentinelai.modules.notification.schemas import (
    NotificationRuleCreate,
    NotificationRuleUpdate,
)
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.logging import log
from sentinelai.platform.notifications import (
    NotificationMessage,
    NotificationSender,
    build_notification_sender,
)
from sentinelai.shared.exceptions import ForbiddenError
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor

# The producing module recorded on each notification — half of the §25.9 idempotency key.
_MODULE_INGESTION = "ingestion"
_MODULE_INVESTIGATION = "investigation"
_MODULE_CASE_MANAGEMENT = "case_management"


class NotificationService:
    def __init__(
        self, uow: NotificationUnitOfWork, *, sender: NotificationSender | None = None
    ) -> None:
        self._uow = uow
        self._sender = sender if sender is not None else build_notification_sender()

    async def list_notifications(
        self, actor: CurrentUser, page: PageParams
    ) -> tuple[list[Notification], str | None, bool]:
        """The caller's OWN notifications, newest first (api-design.md §8).

        Returns ``(items, next_cursor, has_more)``. The recipient is always the authenticated
        actor — never a parameter — so one analyst's inbox is not reachable from another's
        session. The repository fetches ``limit + 1``; the extra row is the ``has_more`` signal
        and is trimmed off before it is returned.
        """
        cursor_created_at: datetime | None = None
        cursor_notification_id: UUID | None = None
        if page.cursor:
            raw_value, cursor_notification_id = decode_cursor(page.cursor)
            cursor_created_at = datetime.fromisoformat(raw_value)

        rows = await self._uow.notifications.list_for_recipient(
            actor.user_id,
            limit=page.limit,
            cursor_created_at=cursor_created_at,
            cursor_notification_id=cursor_notification_id,
        )
        has_more = len(rows) > page.limit
        items = list(rows[: page.limit])
        next_cursor = (
            encode_cursor(items[-1].created_at.isoformat(), items[-1].notification_id)
            if has_more and items
            else None
        )
        return items, next_cursor, has_more

    async def mark_read(self, notification_id: UUID, actor: CurrentUser) -> Notification:
        """Mark the caller's own notification read. Idempotent.

        A second call is a no-op that returns the notification unchanged — the original
        ``read_at`` is preserved, so re-reading never rewrites when the analyst first saw it.

        A notification belonging to someone else raises ``ForbiddenError`` (403), NOT 404 —
        api-design.md §8's deliberate, documented exception to the NOT_FOUND-hides-existence
        convention. Never commits: the entrypoint owns the transaction (ADR-0005).
        """
        notification = await self._uow.notifications.get_by_id(notification_id)
        if notification is None:
            raise NotificationNotFoundError()
        if notification.recipient_user_id != actor.user_id:
            raise ForbiddenError("notification belongs to another recipient")
        if notification.read_at is None:
            notification.read_at = datetime.now(UTC)
        return notification

    async def redeliver(
        self, notification_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> None:
        raise NotImplementedError

    async def list_rules(self, actor: CurrentUser) -> Sequence[NotificationRule]:
        raise NotImplementedError

    async def create_rule(
        self, data: NotificationRuleCreate, actor: CurrentUser, correlation_id: str
    ) -> NotificationRule:
        raise NotImplementedError

    async def update_rule(
        self, rule_id: UUID, data: NotificationRuleUpdate, actor: CurrentUser, expected_etag: str
    ) -> NotificationRule:
        raise NotImplementedError

    # -- consumer-path dispatch (invoked from events.py handlers) -----------
    async def _create_and_dispatch(
        self,
        *,
        recipient_user_id: UUID,
        source_module: str,
        source_reference_id: UUID,
        subject: str,
        body: str,
        correlation_id: str,
        match_message: bool = False,
    ) -> Notification | None:
        """Shared dispatch core for every consumed-event handler.

        Applies the §25.9 business-idempotency check (``match_message=True`` adds the stored
        message to the key, for events like `case.status_changed` whose catalog key carries an
        extra discriminator), persists the notification, hands it to the sender, records the
        delivery outcome, and publishes the outcome event. Returns ``None`` when the analyst
        already has this message. Never commits (ADR-0005): the dispatcher owns the transaction.
        """
        if await self._uow.notifications.exists_for_source(
            recipient_user_id,
            source_module,
            source_reference_id,
            message=body if match_message else None,
        ):
            return None  # §25.9 idempotency key — the analyst already has this message

        notification = Notification(
            rule_id=None,  # system-generated: consumed-event dispatches are not rule-driven
            recipient_user_id=recipient_user_id,
            source_module=source_module,
            source_reference_id=source_reference_id,
            message=body,
            created_at=datetime.now(UTC),
            read_at=None,
        )
        await self._uow.notifications.add(notification)

        message = NotificationMessage(
            recipient_user_id=recipient_user_id,
            subject=subject,
            body=body,
            channel=self._sender.channel,
        )
        # A channel failure must not discard the notification: the in-app row is the durable
        # delivery, so the outcome is recorded and reported rather than raised. Raising would
        # roll back the whole handler transaction — including the inbox claim and this row —
        # and the retry would re-notify.
        try:
            await self._sender.send(message)
            status, delivered_at = "delivered", datetime.now(UTC)
            event_type = EVENT_NOTIFICATION_DISPATCHED
            event_payload: dict[str, object] = {
                "notification_id": str(notification.notification_id),
                "channel": self._sender.channel,
            }
        except Exception as exc:
            log.warning(
                "notification_delivery_failed",
                channel=self._sender.channel,
                notification_id=str(notification.notification_id),
                error=type(exc).__name__,
            )
            status, delivered_at = "failed", None
            event_type = EVENT_NOTIFICATION_DELIVERY_FAILED
            event_payload = {
                "notification_id": str(notification.notification_id),
                "channel": self._sender.channel,
                "error": type(exc).__name__,
            }

        await self._uow.deliveries.add(
            NotificationDelivery(
                notification_id=notification.notification_id,
                channel=self._sender.channel,
                delivery_status=status,
                attempted_at=datetime.now(UTC),
                delivered_at=delivered_at,
            )
        )
        await self._uow.outbox.publish(
            event_type=event_type,
            aggregate_type="notification",
            aggregate_id=notification.notification_id,
            payload=event_payload,
            correlation_id=correlation_id,
            actor_type="system",
        )
        return notification

    async def dispatch_for_evidence_scanned(
        self,
        *,
        evidence_id: UUID,
        recipient_user_id: UUID,
        detection_name: str | None,
        engine: str,
        correlation_id: str,
    ) -> Notification | None:
        """Notify the uploading analyst that a detection blocked their upload (security §25).

        The caller has already decided this scan was a *block*. Idempotency key (§25.9):
        ``(recipient_user_id, source_module='ingestion', source_reference_id=evidence_id)``.
        """
        detection = detection_name or "an unnamed detection"
        return await self._create_and_dispatch(
            recipient_user_id=recipient_user_id,
            source_module=_MODULE_INGESTION,
            source_reference_id=evidence_id,
            subject="Evidence upload blocked by malware scan",
            body=(
                f"Upload blocked: malware detected in evidence {evidence_id} "
                f"({detection}, engine={engine}). The file is retained in quarantine and was "
                f"not promoted to the evidence store."
            ),
            correlation_id=correlation_id,
        )

    async def dispatch_for_correlation_generated(
        self,
        *,
        case_id: UUID,
        relationship_id: UUID,
        recipient_user_id: UUID,
        correlation_id: str,
    ) -> Notification | None:
        """Tell the case's investigator that the AI proposed a new relationship to review.

        Idempotency key (§25.9): ``(recipient_user_id, source_module='investigation',
        source_reference_id=relationship_id)`` — the catalog calls this the tightest key it has,
        because a replayed event must never re-send a review request already delivered.

        The finding stays ``proposed`` until a human reviews it (PRD FR-7.3); this notification
        is the prompt to do so, never an approval.
        """
        return await self._create_and_dispatch(
            recipient_user_id=recipient_user_id,
            source_module=_MODULE_INVESTIGATION,
            source_reference_id=relationship_id,
            subject="New AI-proposed finding awaiting review",
            body=(
                f"A new relationship ({relationship_id}) was proposed for case {case_id} and is "
                f"awaiting your review. It remains proposed until you confirm or reject it."
            ),
            correlation_id=correlation_id,
        )

    async def dispatch_for_case_status_changed(
        self,
        *,
        case_id: UUID,
        new_status: str,
        recipient_user_id: UUID,
        correlation_id: str,
    ) -> Notification | None:
        """Notify the case's investigator of a lifecycle transition.

        Idempotency key (§25.9): ``(recipient_user_id, source_reference_id=case_id, new_status)``
        — the ``new_status`` discriminator is carried by matching the stored message, which is
        composed as a pure function of exactly ``case_id`` and ``new_status`` (no timestamp, no
        previous status), so the comparison is stable. Reaching the same status twice therefore
        notifies once, which is what that key specifies.
        """
        return await self._create_and_dispatch(
            recipient_user_id=recipient_user_id,
            source_module=_MODULE_CASE_MANAGEMENT,
            source_reference_id=case_id,
            subject=f"Case {case_id} is now {new_status}",
            body=f"Case {case_id} status changed to {new_status}.",
            correlation_id=correlation_id,
            match_message=True,  # the key carries new_status beyond (recipient, case)
        )

    async def dispatch_for_report_generated(
        self,
        *,
        case_id: UUID,
        report_id: UUID,
        recipient_user_id: UUID,
        correlation_id: str,
    ) -> Notification | None:
        """Tell the requester their case report is ready to download.

        Idempotency key (§25.9): ``(recipient_user_id, source_reference_id=report_id)`` — scoped
        to the report, so regenerating a case's report is a genuinely new fact and does notify.
        """
        return await self._create_and_dispatch(
            recipient_user_id=recipient_user_id,
            source_module=_MODULE_CASE_MANAGEMENT,
            source_reference_id=report_id,
            subject="Case report ready for download",
            body=(
                f"The report ({report_id}) you requested for case {case_id} has finished "
                f"generating and is ready to download."
            ),
            correlation_id=correlation_id,
        )


def get_notification_service(
    uow: NotificationUnitOfWork = Depends(get_notification_uow),
) -> NotificationService:
    return NotificationService(uow)
