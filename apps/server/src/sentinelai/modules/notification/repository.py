"""notification persistence + Unit of Work (guide Part 3). Persistence only.

Three stores: the rule table (bounded admin configuration), the recipient's notification inbox
(append-heavy, keyset-paginated), and the per-notification delivery attempts.

``list_for_recipient`` is scoped to ``recipient_user_id`` **in SQL**, so no service mistake can
expose one analyst's inbox to another. ``exists_for_source`` is the read half of §25.9's business
idempotency key; ``uq_notification_dedupe`` is the half that survives a race.
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
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter
from sentinelai.platform.events.signing import EventSigner

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
        read: bool | None = None,
    ) -> Sequence[Notification]:
        """The recipient's notifications, newest first, keyset-paginated (api-design.md §2.5).

        Scoped to ``recipient_user_id`` in SQL — a caller can never read another analyst's inbox,
        regardless of what the service does. Returns up to ``limit + 1`` rows so the service can
        compute ``has_more`` without a second COUNT (never offset pagination on an append-heavy
        table). ``(created_at, notification_id)`` is compared as a tuple so the tie-break is
        total: notifications raised in the same transaction share a timestamp.

        The cursor arrives already decoded — opaque-cursor codec is application logic and stays
        in the service, matching every other list repository in the codebase.

        ``read`` is §8's documented boolean filter, expressed against ``read_at`` because that
        column
        *is* the read state — there is no separate flag to drift from it. ``None`` means no filter,
        which is what an absent query parameter means.
        """
        stmt = select(Notification).where(Notification.recipient_user_id == recipient_user_id)
        if read is True:
            stmt = stmt.where(Notification.read_at.is_not(None))
        elif read is False:
            stmt = stmt.where(Notification.read_at.is_(None))
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
        result = await self._session.execute(
            select(NotificationRule).where(NotificationRule.rule_id == rule_id)
        )
        return result.scalar_one_or_none()

    async def add(self, rule: NotificationRule) -> None:
        self._session.add(rule)
        await self._session.flush()

    async def list_(self) -> Sequence[NotificationRule]:
        """Every rule, active and inactive, ordered by name.

        **Unpaginated on purpose.** `database-design.md` §7 classes `notification_rules` as
        reference/config data and `frontend-architecture.md` §21 puts it with the "small, bounded
        lists" that get page numbers rather than cursors — an operator configures a handful of
        rules,
        not a stream of them. `api-design.md` §4.9 gives the endpoint no `cursor`/`limit`, so adding
        pagination here would be inventing contract.

        Inactive rules are included because §7's lifecycle for config data is an `is_active` flag
        rather than deletion: a deactivated rule is still configuration an admin must be able to see
        and re-enable, and hiding it would make the flag look like a delete.

        Ordered by ``(name, rule_id)`` — name because that is what an operator scans, ``rule_id`` to
        break ties so two rules sharing a name do not swap places between reads.
        """
        result = await self._session.execute(
            select(NotificationRule).order_by(NotificationRule.name, NotificationRule.rule_id)
        )
        return result.scalars().all()


class DeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, delivery: NotificationDelivery) -> None:
        self._session.add(delivery)
        await self._session.flush()

    async def list_for_notification(self, notification_id: UUID) -> Sequence[NotificationDelivery]:
        """Every delivery attempt for one notification, **most recent attempt first**.

        Newest-first because the only caller asks a question about the latest attempt — "is there a
        failure to retry" (`api-design.md` §4.9's redeliver endpoint) — and a chronological list
        would make that the last row rather than the first.

        ``attempted_at`` is nullable in §3.6, so ``delivery_id`` breaks the tie and keeps the order
        total; ``nulls_last`` puts an attempt with no recorded time behind ones that have it rather
        than letting it masquerade as the newest.
        """
        result = await self._session.execute(
            select(NotificationDelivery)
            .where(NotificationDelivery.notification_id == notification_id)
            .order_by(
                NotificationDelivery.attempted_at.desc().nulls_last(),
                NotificationDelivery.delivery_id.desc(),
            )
        )
        return result.scalars().all()


class NotificationUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession, *, kms: KeyManagementService | None = None) -> None:
        super().__init__(session)
        self.notifications = NotificationRepository(session)
        self.rules = NotificationRuleRepository(session)
        self.deliveries = DeliveryRepository(session)
        self.outbox = OutboxWriter(
            session,
            schema=_SCHEMA,
            # ADR-0007 §1: events this module publishes are signed under EVENT_ROOT. `None`
            # writes them unsigned, which the dispatcher's strict mode then refuses -- so an
            # unwired publisher fails loudly at consume time rather than silently here.
            signer=EventSigner(kms) if kms is not None else None,
        )


async def get_notification_uow(
    session: AsyncSession = Depends(get_session),
    kms: KeyManagementService = Depends(get_kms),
) -> NotificationUnitOfWork:
    return NotificationUnitOfWork(session, kms=kms)
