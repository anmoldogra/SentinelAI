"""notification persistence + Unit of Work (guide Part 3). Persistence only.

Only the members the ``evidence.scanned`` consumer path needs are implemented; the
notification-inbox and rule-management reads that serve ``router.py`` are still deferred
(``NotImplementedError``) — see the module's phase notes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.notification.models import (
    Notification,
    NotificationDelivery,
    NotificationRule,
)
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter

_SCHEMA = "notification"


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, notification_id: UUID) -> Notification | None:
        result = await self._session.execute(
            select(Notification).where(Notification.notification_id == notification_id)
        )
        return result.scalar_one_or_none()

    async def add(self, notification: Notification) -> None:
        self._session.add(notification)
        await self._session.flush()

    async def exists_for_source(
        self,
        recipient_user_id: UUID,
        source_module: str,
        source_reference_id: UUID,
        *,
        message: str | None = None,
    ) -> bool:
        """Whether this recipient already has a notification for this source fact.

        The business-idempotency key from the §25.9 catalog. Distinct from the Inbox claim: the
        Inbox stops the SAME event being processed twice, this stops two *different* events (a
        re-scan, a replayed upstream fact) producing a second copy of a message the analyst has
        already received.

        ``message`` narrows the key for events whose catalog key carries an extra discriminator —
        `case.status_changed`'s ``(…, case_id, new_status)`` — via exact equality on the stored
        message, which the dispatcher composes as a pure function of exactly those key fields.
        """
        conditions = [
            Notification.recipient_user_id == recipient_user_id,
            Notification.source_module == source_module,
            Notification.source_reference_id == source_reference_id,
        ]
        if message is not None:
            conditions.append(Notification.message == message)
        result = await self._session.execute(
            select(Notification.notification_id).where(*conditions).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_for_recipient(
        self,
        recipient_user_id: UUID,
        *,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_notification_id: UUID | None,
    ) -> Sequence[Notification]:
        """The recipient's notifications, newest first, keyset-paginated (api-design.md §2.5).

        Scoped to ``recipient_user_id`` in SQL — a caller can never read another analyst's inbox,
        regardless of what the service does. Returns up to ``limit + 1`` rows so the service can
        compute ``has_more`` without a second COUNT (never offset pagination on an append-heavy
        table). ``(created_at, notification_id)`` is compared as a tuple so the tie-break is
        total: notifications raised in the same transaction share a timestamp.

        The cursor arrives already decoded — opaque-cursor codec is application logic and stays
        in the service, matching every other list repository in the codebase.
        """
        stmt = select(Notification).where(Notification.recipient_user_id == recipient_user_id)
        if cursor_created_at is not None and cursor_notification_id is not None:
            stmt = stmt.where(
                tuple_(Notification.created_at, Notification.notification_id)
                < (cursor_created_at, cursor_notification_id)
            )
        stmt = stmt.order_by(
            Notification.created_at.desc(), Notification.notification_id.desc()
        ).limit(limit + 1)
        return (await self._session.execute(stmt)).scalars().all()


class NotificationRuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, rule_id: UUID) -> NotificationRule | None:
        raise NotImplementedError

    async def add(self, rule: NotificationRule) -> None:
        raise NotImplementedError

    async def list_(self) -> Sequence[NotificationRule]:
        raise NotImplementedError


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, delivery: NotificationDelivery) -> None:
        self._session.add(delivery)
        await self._session.flush()

    async def list_for_notification(self, notification_id: UUID) -> Sequence[NotificationDelivery]:
        raise NotImplementedError


class NotificationUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.notifications = NotificationRepository(session)
        self.rules = NotificationRuleRepository(session)
        self.deliveries = DeliveryRepository(session)
        self.outbox = OutboxWriter(session, schema=_SCHEMA)


async def get_notification_uow(
    session: AsyncSession = Depends(get_session),
) -> NotificationUnitOfWork:
    return NotificationUnitOfWork(session)
