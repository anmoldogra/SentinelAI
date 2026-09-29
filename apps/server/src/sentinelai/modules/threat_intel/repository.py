"""threat_intel persistence + Unit of Work (guide Part 3). Persistence only.

The one query worth reading closely is :meth:`IocRepository.find_by_values`. Matching runs on every
ingested evidence item, so it is written as **one indexed lookup against a token set** rather than a
loop over the IOC library — the loop gets slower as the threat library grows, which is the wrong
scaling for the thing on the hot path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.threat_intel.models import (
    FeedSubscription,
    Ioc,
    IocEvidenceMatch,
    ThreatActorProfile,
)
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter
from sentinelai.platform.events.signing import EventSigner

_SCHEMA = "threat_intel"

# An IOC's lifecycle status. `database-design.md` §3.3 requires the column and does not fix its
# vocabulary; `active` is the state matching considers, and a retired indicator stops producing
# matches without being deleted — deleting it would lose the matches already recorded against it.
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"


class IocRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, ioc_id: UUID) -> Ioc | None:
        result = await self._session.execute(select(Ioc).where(Ioc.ioc_id == ioc_id))
        return result.scalar_one_or_none()

    async def add(self, ioc: Ioc) -> None:
        self._session.add(ioc)
        await self._session.flush()

    async def find_by_type_and_value(self, indicator_type: str, value: str) -> Ioc | None:
        """Resolve an existing IOC by its identity, for re-registration.

        `(indicator_type, value)` is an IOC's natural key: the same indicator arriving from a second
        feed is the same indicator. Used to make registration converge rather than accumulate
        duplicates, which would each match the same evidence separately and multiply one hit into
        several alerts.
        """
        result = await self._session.execute(
            select(Ioc).where(Ioc.indicator_type == indicator_type, Ioc.value == value)
        )
        return result.scalars().first()

    def _page(self, *, limit: int, after: tuple[datetime, UUID] | None) -> Select[tuple[Ioc]]:
        stmt = select(Ioc)
        if after is not None:
            collected_at, ioc_id = after
            stmt = stmt.where(tuple_(Ioc.collected_at, Ioc.ioc_id) > (collected_at, ioc_id))
        return stmt.order_by(Ioc.collected_at.asc(), Ioc.ioc_id.asc()).limit(limit)

    async def list_(
        self, *, limit: int, after: tuple[datetime, UUID] | None = None
    ) -> Sequence[Ioc]:
        result = await self._session.execute(self._page(limit=limit, after=after))
        return result.scalars().all()

    async def find_by_values(self, values: Sequence[str]) -> Sequence[Ioc]:
        """Every **active** IOC whose normalized value appears in ``values``.

        This is the matching engine's only query, and its shape is the design decision: the caller
        reduces one evidence object to a token set and asks the database which indicators are in it.
        One indexed `IN` beats iterating the IOC library and testing each against the evidence, and
        the difference grows with the library rather than with the evidence.

        Retired IOCs are excluded here rather than filtered afterwards — a retired indicator should
        cost nothing on the hot path, and matching on one would resurrect an indicator an analyst
        deliberately stood down.
        """
        if not values:
            return []
        result = await self._session.execute(
            select(Ioc).where(Ioc.status == STATUS_ACTIVE, Ioc.value.in_(values))
        )
        return result.scalars().all()


class ThreatActorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, threat_actor_id: UUID) -> ThreatActorProfile | None:
        result = await self._session.execute(
            select(ThreatActorProfile).where(ThreatActorProfile.threat_actor_id == threat_actor_id)
        )
        return result.scalar_one_or_none()

    async def add(self, profile: ThreatActorProfile) -> None:
        self._session.add(profile)
        await self._session.flush()

    async def list_(self) -> Sequence[ThreatActorProfile]:
        result = await self._session.execute(
            select(ThreatActorProfile).order_by(ThreatActorProfile.name.asc())
        )
        return result.scalars().all()


class FeedSubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, subscription_id: UUID) -> FeedSubscription | None:
        result = await self._session.execute(
            select(FeedSubscription).where(FeedSubscription.subscription_id == subscription_id)
        )
        return result.scalar_one_or_none()

    async def add(self, subscription: FeedSubscription) -> None:
        self._session.add(subscription)
        await self._session.flush()

    async def find_by_name(self, feed_name: str) -> FeedSubscription | None:
        """Resolve a subscription by feed name — its natural key, for idempotent re-adding."""
        result = await self._session.execute(
            select(FeedSubscription).where(FeedSubscription.feed_name == feed_name)
        )
        return result.scalars().first()

    async def list_(self) -> Sequence[FeedSubscription]:
        result = await self._session.execute(
            select(FeedSubscription).order_by(FeedSubscription.feed_name.asc())
        )
        return result.scalars().all()


class IocMatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, match: IocEvidenceMatch) -> None:
        self._session.add(match)
        await self._session.flush()

    async def exists_for_pair(self, *, ioc_id: UUID, matched_evidence_id: UUID) -> bool:
        """Whether this `(ioc_id, matched_evidence_id)` pair is already recorded.

        §25.4 names that pair as the handler's idempotency key: "never create a duplicate match row
        for the same pair". Checked here *and* enforced by a unique index, for the same
        belt-and-suspenders reason ADR-0011 §4 gives — the check keeps the common case quiet, the
        constraint makes a concurrent second scan impossible rather than merely unlikely.
        """
        result = await self._session.execute(
            select(IocEvidenceMatch.match_id).where(
                IocEvidenceMatch.ioc_id == ioc_id,
                IocEvidenceMatch.matched_evidence_id == matched_evidence_id,
            )
        )
        return result.scalars().first() is not None

    async def list_for_ioc(
        self, ioc_id: UUID, *, limit: int, after: tuple[datetime, UUID] | None = None
    ) -> Sequence[IocEvidenceMatch]:
        """One page of matches for an IOC, newest first (api-design.md §4.4: "`matched_at` desc").

        Descending here, unlike the ascending keyset elsewhere, because §4.4 specifies it and
        because
        an analyst reading an indicator's matches wants the most recent sighting first. The cursor
        comparison flips with the order — ``<`` rather than ``>`` — which is the detail that
        silently
        returns an empty second page if it is copied from an ascending list.
        """
        stmt = select(IocEvidenceMatch).where(IocEvidenceMatch.ioc_id == ioc_id)
        if after is not None:
            matched_at, match_id = after
            stmt = stmt.where(
                tuple_(IocEvidenceMatch.matched_at, IocEvidenceMatch.match_id)
                < (matched_at, match_id)
            )
        stmt = stmt.order_by(
            IocEvidenceMatch.matched_at.desc(), IocEvidenceMatch.match_id.desc()
        ).limit(limit)
        result = await self._session.execute(stmt)
        return result.scalars().all()


class ThreatIntelUnitOfWork(UnitOfWork):
    def __init__(self, session: AsyncSession, *, kms: KeyManagementService | None = None) -> None:
        super().__init__(session)
        self.iocs = IocRepository(session)
        self.threat_actors = ThreatActorRepository(session)
        self.feeds = FeedSubscriptionRepository(session)
        self.matches = IocMatchRepository(session)
        self.outbox = OutboxWriter(
            session,
            schema=_SCHEMA,
            # ADR-0007 §1: events this module publishes are signed under EVENT_ROOT. `None`
            # writes them unsigned, which the dispatcher's strict mode then refuses -- so an
            # unwired publisher fails loudly at consume time rather than silently here.
            signer=EventSigner(kms) if kms is not None else None,
        )


async def get_threat_intel_uow(
    session: AsyncSession = Depends(get_session),
    kms: KeyManagementService = Depends(get_kms),
) -> ThreatIntelUnitOfWork:
    return ThreatIntelUnitOfWork(session, kms=kms)
