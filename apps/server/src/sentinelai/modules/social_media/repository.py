"""social_media persistence + Unit of Work (guide Part 3). Persistence only."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends
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
