"""threat_intel business logic (guide Part 5) — IOC/actor/feed management and the
IOC-matching that reacts to newly ingested evidence. Bodies deferred."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.threat_intel.models import (
    FeedSubscription,
    Ioc,
    IocEvidenceMatch,
    ThreatActorProfile,
)
from sentinelai.modules.threat_intel.repository import ThreatIntelUnitOfWork, get_threat_intel_uow
from sentinelai.modules.threat_intel.schemas import FeedCreate, IocCreate, ThreatActorCreate
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.pagination import PageParams


class ThreatIntelService:
    def __init__(self, uow: ThreatIntelUnitOfWork) -> None:
        self._uow = uow

    async def list_iocs(self, actor: CurrentUser, page: PageParams) -> Sequence[Ioc]:
        raise NotImplementedError

    async def register_ioc(self, data: IocCreate, actor: CurrentUser, correlation_id: str) -> Ioc:
        raise NotImplementedError

    async def get_ioc(self, ioc_id: UUID, actor: CurrentUser) -> Ioc:
        raise NotImplementedError

    async def list_matches(self, ioc_id: UUID, actor: CurrentUser) -> Sequence[IocEvidenceMatch]:
        raise NotImplementedError

    async def list_threat_actors(self, actor: CurrentUser) -> Sequence[ThreatActorProfile]:
        raise NotImplementedError

    async def create_threat_actor(
        self, data: ThreatActorCreate, actor: CurrentUser, correlation_id: str
    ) -> ThreatActorProfile:
        raise NotImplementedError

    async def list_feeds(self, actor: CurrentUser) -> Sequence[FeedSubscription]:
        raise NotImplementedError

    async def add_feed(
        self, data: FeedCreate, actor: CurrentUser, correlation_id: str
    ) -> FeedSubscription:
        raise NotImplementedError

    async def sync_feed(self, subscription_id: UUID, actor: CurrentUser, correlation_id: str) -> None:
        """Enqueue an on-demand feed sync job (async, §25.4)."""
        raise NotImplementedError

    async def scan_evidence_for_matches(self, evidence_id: UUID, category: str, correlation_id: str) -> None:
        """Consumer path: scan new evidence against active IOCs; publish a match per hit."""
        raise NotImplementedError


def get_threat_intel_service(
    uow: ThreatIntelUnitOfWork = Depends(get_threat_intel_uow),
) -> ThreatIntelService:
    return ThreatIntelService(uow)
