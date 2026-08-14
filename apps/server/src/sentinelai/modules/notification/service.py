"""notification business logic (guide Part 5) — rule management, the caller's
notification inbox, and dispatch driven by consumed events. Bodies deferred.

Dispatch handlers enforce the catalog's tight business-idempotency keys (§25.9) so
a replayed event never re-sends a message the analyst already received.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.notification.models import Notification, NotificationRule
from sentinelai.modules.notification.repository import NotificationUnitOfWork, get_notification_uow
from sentinelai.modules.notification.schemas import (
    NotificationRuleCreate,
    NotificationRuleUpdate,
)
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.pagination import PageParams


class NotificationService:
    def __init__(self, uow: NotificationUnitOfWork) -> None:
        self._uow = uow

    async def list_notifications(
        self, actor: CurrentUser, page: PageParams
    ) -> Sequence[Notification]:
        raise NotImplementedError

    async def mark_read(self, notification_id: UUID, actor: CurrentUser) -> Notification:
        raise NotImplementedError

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
    async def dispatch_for_correlation_generated(
        self, case_id: UUID, relationship_id: UUID, correlation_id: str
    ) -> None:
        raise NotImplementedError

    async def dispatch_for_case_status_changed(
        self, case_id: UUID, new_status: str, correlation_id: str
    ) -> None:
        raise NotImplementedError

    async def dispatch_for_report_generated(
        self, case_id: UUID, report_id: UUID, correlation_id: str
    ) -> None:
        raise NotImplementedError


def get_notification_service(
    uow: NotificationUnitOfWork = Depends(get_notification_uow),
) -> NotificationService:
    return NotificationService(uow)
