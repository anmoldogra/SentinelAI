"""threat_intel HTTP routes — api-design.md §5 (Threat Intel). Parse and delegate only."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from sentinelai.modules.threat_intel.schemas import (
    FeedCreate,
    FeedRead,
    IocCreate,
    IocRead,
    MatchRead,
    ThreatActorCreate,
    ThreatActorRead,
)
from sentinelai.modules.threat_intel.service import ThreatIntelService, get_threat_intel_service
from sentinelai.platform.auth.dependencies import CurrentUser, require_role
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(prefix="/api/v1/threat-intel", tags=["threat-intel"])


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


@router.get("/iocs", response_model=ListEnvelope[IocRead])
async def list_iocs(
    request: Request,
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> ListEnvelope[IocRead]:
    items = await service.list_iocs(current_user, page)
    return ListEnvelope(
        data=[IocRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=page.limit),
        meta=_meta(request),
    )


@router.post("/iocs", response_model=Envelope[IocRead], status_code=status.HTTP_201_CREATED)
async def register_ioc(
    payload: IocCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> Envelope[IocRead]:
    ioc = await service.register_ioc(payload, current_user, request.state.correlation_id)
    return Envelope(data=IocRead.model_validate(ioc), meta=_meta(request))


@router.get("/iocs/{ioc_id}", response_model=Envelope[IocRead])
async def get_ioc(
    ioc_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> Envelope[IocRead]:
    ioc = await service.get_ioc(ioc_id, current_user)
    return Envelope(data=IocRead.model_validate(ioc), meta=_meta(request))


@router.get("/iocs/{ioc_id}/matches", response_model=ListEnvelope[MatchRead])
async def list_ioc_matches(
    ioc_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> ListEnvelope[MatchRead]:
    items = await service.list_matches(ioc_id, current_user)
    return ListEnvelope(
        data=[MatchRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.get("/threat-actors", response_model=ListEnvelope[ThreatActorRead])
async def list_threat_actors(
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> ListEnvelope[ThreatActorRead]:
    items = await service.list_threat_actors(current_user)
    return ListEnvelope(
        data=[ThreatActorRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post(
    "/threat-actors", response_model=Envelope[ThreatActorRead], status_code=status.HTTP_201_CREATED
)
async def create_threat_actor(
    payload: ThreatActorCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "admin")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> Envelope[ThreatActorRead]:
    actor = await service.create_threat_actor(payload, current_user, request.state.correlation_id)
    return Envelope(data=ThreatActorRead.model_validate(actor), meta=_meta(request))


@router.get("/feeds", response_model=ListEnvelope[FeedRead])
async def list_feeds(
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> ListEnvelope[FeedRead]:
    items = await service.list_feeds(current_user)
    return ListEnvelope(
        data=[FeedRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post("/feeds", response_model=Envelope[FeedRead], status_code=status.HTTP_201_CREATED)
async def add_feed(
    payload: FeedCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> Envelope[FeedRead]:
    feed = await service.add_feed(payload, current_user, request.state.correlation_id)
    return Envelope(data=FeedRead.model_validate(feed), meta=_meta(request))


@router.post("/feeds/{subscription_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_feed(
    subscription_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: ThreatIntelService = Depends(get_threat_intel_service),
) -> Envelope[dict[str, str]]:
    await service.sync_feed(subscription_id, current_user, request.state.correlation_id)
    return Envelope(data={"status": "accepted"}, meta=_meta(request))
