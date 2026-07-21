"""notification persistence + Unit of Work (guide Part 3). Persistence only."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends
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
        raise NotImplementedError

    async def add(self, notification: Notification) -> None:
        raise NotImplementedError

    async def list_for_recipient(
        self, recipient_user_id: UUID, *, limit: int, cursor: str | None
    ) -> Sequence[Notification]:
        raise NotImplementedError


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
        raise NotImplementedError

    async def list_for_notification(self, notification_id: UUID) -> Sequence[NotificationDelivery]:
        raise NotImplementedError


class NotificationUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.notifications = NotificationRepository(session)
        self.rules = NotificationRuleRepository(session)
        self.deliveries = DeliveryRepository(session)
        self.outbox = OutboxWriter(session, schema=_SCHEMA)


async def get_notification_uow(session: AsyncSession = Depends(get_session)) -> NotificationUnitOfWork:
    return NotificationUnitOfWork(session)
