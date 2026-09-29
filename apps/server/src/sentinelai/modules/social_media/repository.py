"""social_media persistence + Unit of Work (guide Part 3). Persistence only."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.social_media.models import CapturedContent, SocialAccountObserved
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter
from sentinelai.platform.events.signing import EventSigner

_SCHEMA = "social_media"


class ContentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, content_id: UUID) -> CapturedContent | None:
        result = await self._session.execute(
            select(CapturedContent).where(CapturedContent.content_id == content_id)
        )
        return result.scalar_one_or_none()

    async def add(self, content: CapturedContent) -> None:
        self._session.add(content)
        await self._session.flush()

    def _page(
        self, *, limit: int, after: tuple[datetime, UUID] | None
    ) -> Select[tuple[CapturedContent]]:
        stmt = select(CapturedContent)
        if after is not None:
            collected_at, content_id = after
            # Row-value comparison, so the composite key acts as one cursor rather than two
            # independent conditions — "strictly after this (timestamp, id) pair".
            stmt = stmt.where(
                tuple_(CapturedContent.collected_at, CapturedContent.content_id)
                > (collected_at, content_id)
            )
        return stmt.order_by(
            CapturedContent.collected_at.asc(), CapturedContent.content_id.asc()
        ).limit(limit)

    async def list_(
        self, *, limit: int, after: tuple[datetime, UUID] | None = None
    ) -> Sequence[CapturedContent]:
        """One page of captured content, oldest first.

        Ascending for the reason the other connectors give: an analyst works a backlog forward, and
        with newest-first every poll changes what "page 2" holds. A monitored account can produce a
        burst of content inside one second, so ``content_id`` breaking ties is load-bearing here
        rather than decorative.
        """
        result = await self._session.execute(self._page(limit=limit, after=after))
        return result.scalars().all()


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, account_id: UUID) -> SocialAccountObserved | None:
        result = await self._session.execute(
            select(SocialAccountObserved).where(SocialAccountObserved.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def find_by_platform_and_handle(
        self, platform: str, handle: str
    ) -> SocialAccountObserved | None:
        """The one account with this handle on this platform, or ``None``.

        Backed by ``uq_social_account_platform_handle``, which is what lets this be
        ``scalar_one_or_none`` rather than a defensive "first of possibly many": the pair really is
        unique, so a second row would be a constraint violation rather than something to tolerate.
        """
        result = await self._session.execute(
            select(SocialAccountObserved).where(
                SocialAccountObserved.platform == platform,
                SocialAccountObserved.handle == handle,
            )
        )
        return result.scalar_one_or_none()

    async def add(self, account: SocialAccountObserved) -> None:
        self._session.add(account)
        await self._session.flush()

    async def list_(self) -> Sequence[SocialAccountObserved]:
        """Every monitored account.

        Unpaginated, matching the route (`api-design.md` §4.6 gives `GET /social-media/accounts` no
        pagination): the monitoring list is analyst-curated, bounded by how many accounts a case
        watches rather than by data volume — unlike the content those accounts produce, which is
        paginated. Ordered by platform then handle so the console does not reshuffle between reads.
        """
        result = await self._session.execute(
            select(SocialAccountObserved).order_by(
                SocialAccountObserved.platform.asc(), SocialAccountObserved.handle.asc()
            )
        )
        return result.scalars().all()


class SocialMediaUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession, *, kms: KeyManagementService | None = None) -> None:
        super().__init__(session)
        self.content = ContentRepository(session)
        self.accounts = AccountRepository(session)
        self.outbox = OutboxWriter(
            session,
            schema=_SCHEMA,
            # ADR-0007 §1: events this module publishes are signed under EVENT_ROOT. `None`
            # writes them unsigned, which the dispatcher's strict mode then refuses -- so an
            # unwired publisher fails loudly at consume time rather than silently here.
            signer=EventSigner(kms) if kms is not None else None,
        )


async def get_social_media_uow(
    session: AsyncSession = Depends(get_session),
    kms: KeyManagementService = Depends(get_kms),
) -> SocialMediaUnitOfWork:
    return SocialMediaUnitOfWork(session, kms=kms)
