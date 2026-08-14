"""social_media persistence + Unit of Work (guide Part 3). Persistence only."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.social_media.models import CapturedContent, SocialAccountObserved
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter

_SCHEMA = "social_media"


class ContentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, content_id: UUID) -> CapturedContent | None:
        raise NotImplementedError

    async def add(self, content: CapturedContent) -> None:
        raise NotImplementedError

    async def list_(self, *, limit: int, cursor: str | None) -> Sequence[CapturedContent]:
        raise NotImplementedError


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, account_id: UUID) -> SocialAccountObserved | None:
        raise NotImplementedError

    async def add(self, account: SocialAccountObserved) -> None:
        raise NotImplementedError

    async def list_(self) -> Sequence[SocialAccountObserved]:
        raise NotImplementedError


class SocialMediaUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.content = ContentRepository(session)
        self.accounts = AccountRepository(session)
        self.outbox = OutboxWriter(session, schema=_SCHEMA)


async def get_social_media_uow(
    session: AsyncSession = Depends(get_session),
) -> SocialMediaUnitOfWork:
    return SocialMediaUnitOfWork(session)
