"""threat_intel business logic (guide Part 5) — IOC/actor/feed management and the IOC matching that
reacts to newly ingested evidence.

Every method here is a **user-facing, audited action** reached through the HTTP router, which is
why the class requires a KMS: each one writes a signed `platform.audit_log` entry.

IOC **matching** is deliberately not here. It runs on the consumer path, where the dispatcher hands
a handler a session and a signed outbox and nothing else — no KMS, no object storage — so it lives
in `events.py` beside the handler that calls it, which is where every other module keeps its
consumer logic. A match is also not an audited user action: no principal did it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.threat_intel.events import EVENT_IOC_REGISTERED
from sentinelai.modules.threat_intel.exceptions import (
    FeedSubscriptionNotFoundError,
    IocNotFoundError,
    ThreatActorNotFoundError,
)
from sentinelai.modules.threat_intel.matching import (
    INDICATOR_TYPES,
    InvalidIndicator,
    normalize_indicator,
)
from sentinelai.modules.threat_intel.models import (
    FeedSubscription,
    Ioc,
    IocEvidenceMatch,
    ThreatActorProfile,
)
from sentinelai.modules.threat_intel.repository import (
    STATUS_ACTIVE,
    IocRepository,
    ThreatIntelUnitOfWork,
    get_threat_intel_uow,
)
from sentinelai.modules.threat_intel.schemas import (
    FeedCreate,
    IocCreate,
    IocRead,
    ThreatActorCreate,
)
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.tasks import TaskQueue, get_task_queue
from sentinelai.shared.exceptions import ValidationFailedError
from sentinelai.shared.pagination import PageParams, decode_cursor

_MODULE = "threat_intel"


class ThreatIntelService:
    def __init__(
        self,
        uow: ThreatIntelUnitOfWork,
        *,
        kms: KeyManagementService,
        tasks: TaskQueue | None = None,
    ) -> None:
        self._uow = uow
        # Required: every audit entry is signed (ADR-0003 §1), so an optional KMS would make an
        # unsigned one reachable. Every method on this class is a user-facing, audited action — IOC
        # matching is deliberately not one, and lives in `events.py`'s
        # `scan_evidence_for_matches`, because a consumer has no KMS to give it.
        self._kms = kms
        self._tasks = tasks

    # -- IOCs ---------------------------------------------------------------
    async def list_iocs(self, actor: CurrentUser, page: PageParams) -> Sequence[Ioc]:
        after: tuple[datetime, UUID] | None = None
        if page.cursor is not None:
            sort_value, last_id = decode_cursor(page.cursor)
            after = (datetime.fromisoformat(sort_value), last_id)
        return await self._uow.iocs.list_(limit=page.limit, after=after)

    async def get_ioc(self, ioc_id: UUID, actor: CurrentUser) -> Ioc:
        ioc = await self._uow.iocs.get_by_id(ioc_id)
        if ioc is None:
            raise IocNotFoundError()
        return ioc

    async def register_ioc(self, data: IocCreate, actor: CurrentUser, correlation_id: str) -> Ioc:
        """Register an indicator (api-design.md §4.4).

        The value is **normalized before it is stored**, which is what makes matching a set lookup
        later: the stored form and the tokens extracted from evidence are put in the same shape
        once,
        so comparison is equality on an indexed column rather than a transformation per IOC per
        ingest.

        **Re-registering the same indicator converges on the existing row** instead of inserting a
        second. `(indicator_type, value)` is an IOC's natural key — the same hash arriving from a
        second feed is the same hash — and two rows would each match the same evidence separately,
        turning one sighting into two alerts. `last_seen` is advanced, because a second feed
        reporting it is new information about when it was current.
        """
        if data.indicator_type not in INDICATOR_TYPES:
            raise ValidationFailedError(
                [
                    {
                        "field": "indicator_type",
                        "message": f"must be one of {sorted(INDICATOR_TYPES)}",
                    }
                ]
            )
        try:
            normalized = normalize_indicator(data.indicator_type, data.value)
        except InvalidIndicator as exc:
            # §4.4: "`value` format validated against `indicator_type`". The message names what was
            # wrong, because a feed author fixing a rejected indicator needs to know which.
            raise ValidationFailedError([{"field": "value", "message": str(exc)}]) from exc

        if data.threat_actor_id is not None and (
            await self._uow.threat_actors.get_by_id(data.threat_actor_id) is None
        ):
            # §4.4: "`threat_actor_id`, if present, must reference an existing profile."
            raise ThreatActorNotFoundError()

        now = datetime.now(UTC)
        existing = await self._uow.iocs.find_by_type_and_value(data.indicator_type, normalized)
        if existing is not None:
            existing.last_seen = now
            if data.threat_actor_id is not None:
                # Late attribution is normal: an indicator is often seen before it is attributed.
                existing.threat_actor_id = data.threat_actor_id
            await self._audit(actor, "ioc_reregistered", existing.ioc_id, {"value": normalized})
            return existing

        ioc = Ioc(
            evidence_id=None,
            status=STATUS_ACTIVE,
            collected_at=now,
            indicator_type=data.indicator_type,
            value=normalized,
            threat_actor_id=data.threat_actor_id,
            first_seen=now,
            last_seen=now,
        )
        await self._uow.iocs.add(ioc)

        # §25.4 publishes `threat_intel.ioc_registered` on "New IOC created". api-design.md §4.4's
        # table says "Events Published: none at creation" — the two documents disagree, and
        # `event-driven-architecture.md` is the authority for the event catalog (CLAUDE.md), so the
        # event is published and the conflict is recorded in the implementation log rather than
        # resolved silently in one direction.
        await self._uow.outbox.publish(
            event_type=EVENT_IOC_REGISTERED,
            aggregate_type="ioc",
            aggregate_id=ioc.ioc_id,
            payload={
                "ioc_id": str(ioc.ioc_id),
                "indicator_type": ioc.indicator_type,
                "value": ioc.value,
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(actor, "ioc_registered", ioc.ioc_id, {"value": normalized})
        return ioc

    async def list_matches(
        self, ioc_id: UUID, actor: CurrentUser, page: PageParams | None = None
    ) -> Sequence[IocEvidenceMatch]:
        """Evidence this IOC has matched, newest first (api-design.md §4.4).

        The IOC must exist: §4.4 validates `ioc_id`, and an empty list for an unknown id would tell
        a
        caller "no matches" when the honest answer is "no such indicator".
        """
        await self.get_ioc(ioc_id, actor)
        params = page or PageParams(limit=50, cursor=None)
        after: tuple[datetime, UUID] | None = None
        if params.cursor is not None:
            sort_value, last_id = decode_cursor(params.cursor)
            after = (datetime.fromisoformat(sort_value), last_id)
        return await self._uow.matches.list_for_ioc(ioc_id, limit=params.limit, after=after)

    # -- threat actors ------------------------------------------------------
    async def list_threat_actors(self, actor: CurrentUser) -> Sequence[ThreatActorProfile]:
        return await self._uow.threat_actors.list_()

    async def create_threat_actor(
        self, data: ThreatActorCreate, actor: CurrentUser, correlation_id: str
    ) -> ThreatActorProfile:
        """Create a threat actor profile (api-design.md §4.4).

        **Publishes nothing.** §25.4's catalog lists exactly two published events for this module,
        `ioc_registered` and `ioc_matched`; there is no `threat_intel.actor_profiled`, and inventing
        one would violate `CLAUDE.md` rule 1 — a new event type must be added to §25's catalog in
        the
        same change that introduces it in code, and no consumer needs it. The creation is audited,
        which is what §4.4 asks for.
        """
        profile = ThreatActorProfile(
            name=data.name, aliases=data.aliases, description=data.description
        )
        await self._uow.threat_actors.add(profile)
        await self._audit(
            actor, "threat_actor_profiled", profile.threat_actor_id, {"name": data.name}
        )
        return profile

    # -- feeds --------------------------------------------------------------
    async def list_feeds(self, actor: CurrentUser) -> Sequence[FeedSubscription]:
        return await self._uow.feeds.list_()

    async def add_feed(
        self, data: FeedCreate, actor: CurrentUser, correlation_id: str
    ) -> FeedSubscription:
        """Add a feed subscription. Re-adding the same `feed_name` converges on the existing row.

        Two subscriptions to one feed would sync it twice and register every indicator twice — which
        the IOC natural key would then collapse, but only after doing the work.
        """
        existing = await self._uow.feeds.find_by_name(data.feed_name)
        if existing is not None:
            existing.protocol = data.protocol
            existing.is_active = True
            await self._audit(
                actor,
                "feed_subscription_updated",
                existing.subscription_id,
                {"feed": data.feed_name},
            )
            return existing

        subscription = FeedSubscription(
            feed_name=data.feed_name, protocol=data.protocol, is_active=True, last_synced_at=None
        )
        await self._uow.feeds.add(subscription)
        await self._audit(
            actor, "feed_subscription_added", subscription.subscription_id, {"feed": data.feed_name}
        )
        return subscription

    async def sync_feed(
        self, subscription_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> None:
        """Enqueue an on-demand feed sync (api-design.md §4.4, async → `202`).

        **Refused outright on a zero-egress profile.** A feed sync is by definition an outbound call
        to a third party, and `deployment-architecture.md` requires air-gapped and classified
        deployments to have "zero configured or observed egress paths". Enqueuing a job that would
        attempt one — and fail, or worse, succeed through a misconfigured proxy — is not an
        acceptable answer on those profiles; refusing at the API boundary is.

        The enqueue is the whole of what this method does. The **feed transport itself is not
        built** (`jobs.sync_feed_subscription`): STIX/TAXII and vendor-API clients are a connector
        increment of their own, and this method exists so the subscription and its trigger are real
        while that is honest about being absent.
        """
        subscription = await self._uow.feeds.get_by_id(subscription_id)
        if subscription is None:
            raise FeedSubscriptionNotFoundError()
        if not subscription.is_active:
            raise ValidationFailedError(
                [{"field": "subscription_id", "message": "subscription is inactive"}]
            )
        if settings.is_air_gapped:
            raise ValidationFailedError(
                [
                    {
                        "field": "subscription_id",
                        "message": (
                            f"feed synchronization requires egress, which the "
                            f"'{settings.app_env}' profile forbids"
                        ),
                    }
                ]
            )

        if self._tasks is not None:
            await self._tasks.enqueue_job("sync_feed_subscription", subscription_id)
        await self._audit(
            actor,
            "feed_sync_requested",
            subscription_id,
            {"feed": subscription.feed_name},
        )

    # -- internals ----------------------------------------------------------
    async def _audit(
        self, actor: CurrentUser, action: str, target_id: UUID, details: dict[str, object]
    ) -> None:
        await record_audit_event(
            self._uow.session,
            kms=self._kms,
            actor_user_id=actor.user_id,
            actor_role=actor.roles[0] if actor.roles else "none",
            action=action,
            module=_MODULE,
            target_type="ioc",
            target_id=target_id,
            details=details,
        )


async def read_ioc(session: AsyncSession, ioc_id: UUID) -> IocRead | None:
    """Cross-module hook: one IOC as its owning module describes it, or ``None`` if it is gone.

    This is the "thin event + reference" fetch `event-driven-architecture.md` §174 specifies.
    `threat_intel.ioc_matched` carries `ioc_id`, `indicator_type`, `confidence` and `matched_at` —
    §25's payload schema for it in full — but **not the indicator's value**, and
    `investigation` needs that value to name the `digital_asset` entity a match produces. §174's
    answer to exactly this is that "a consumer that needs more fetches the full object via the
    owning module's public interface", which is cheaper than a payload change: adding a sixth field
    would be a MINOR bump (§7) to a contract the document pins, and would put indicator values on
    the bus, which §21 asks us to avoid where a fetch will do.

    **A function over a session, not a ``ThreatIntelService`` method**, for the same reason
    `ingestion.public.read_evidence_attributes` is one: the dispatcher hands a handler a session and
    a signed outbox, and every method on the service is an audited user action requiring a KMS this
    read never touches. It takes no actor because no principal is asking — a consumer that
    fabricated one would be lying to every audit path it reached.

    Returns the Pydantic schema rather than the ORM row: a caller in another module must not hold a
    `threat_intel` model (guide Part 1), and `IocRead` is already this module's public shape for it.
    """
    ioc = await IocRepository(session).get_by_id(ioc_id)
    return IocRead.model_validate(ioc) if ioc is not None else None


def get_threat_intel_service(
    uow: ThreatIntelUnitOfWork = Depends(get_threat_intel_uow),
    kms: KeyManagementService = Depends(get_kms),
    tasks: TaskQueue = Depends(get_task_queue),
) -> ThreatIntelService:
    return ThreatIntelService(uow, kms=kms, tasks=tasks)
