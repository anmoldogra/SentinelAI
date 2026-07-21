"""threat_intel persistence + Unit of Work (guide Part 3). Persistence only."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.threat_intel.models import (
    FeedSubscription,
    Ioc,
    IocEvidenceMatch,
    ThreatActorProfile,
)
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter

_SCHEMA = "threat_intel"


class IocRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, ioc_id: UUID) -> Ioc | None:
        raise NotImplementedError

    async def add(self, ioc: Ioc) -> None:
        raise NotImplementedError

    async def list_(self, *, limit: int, cursor: str | None) -> Sequence[Ioc]:
        raise NotImplementedError

    async def find_matching(self, category: str, evidence_id: UUID) -> Sequence[Ioc]:
        """Active IOCs present in newly ingested evidence (consumer path, §25.4)."""
        raise NotImplementedError


class ThreatActorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, threat_actor_id: UUID) -> ThreatActorProfile | None:
        raise NotImplementedError

    async def add(self, actor: ThreatActorProfile) -> None:
        raise NotImplementedError

    async def list_(self) -> Sequence[ThreatActorProfile]:
        raise NotImplementedError


class FeedRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, subscription_id: UUID) -> FeedSubscription | None:
        raise NotImplementedError

    async def add(self, feed: FeedSubscription) -> None:
        raise NotImplementedError

    async def list_(self) -> Sequence[FeedSubscription]:
        raise NotImplementedError


class MatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, match: IocEvidenceMatch) -> None:
        raise NotImplementedError

    async def list_for_ioc(self, ioc_id: UUID) -> Sequence[IocEvidenceMatch]:
        raise NotImplementedError


class ThreatIntelUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.iocs = IocRepository(session)
        self.threat_actors = ThreatActorRepository(session)
        self.feeds = FeedRepository(session)
        self.matches = MatchRepository(session)
        self.outbox = OutboxWriter(session, schema=_SCHEMA)


async def get_threat_intel_uow(session: AsyncSession = Depends(get_session)) -> ThreatIntelUnitOfWork:
    return ThreatIntelUnitOfWork(session)
