"""osint HTTP routes — api-design.md §5 (OSINT). Parse and delegate only."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status

from sentinelai.modules.osint.schemas import (
    FindingCreate,
    FindingRead,
    SourceCreate,
    SourceRead,
    SourceUpdate,
)
from sentinelai.modules.osint.service import OsintService, get_osint_service
from sentinelai.platform.auth.dependencies import CurrentUser, require_role
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(prefix="/api/v1/osint", tags=["osint"])


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


@router.get("/sources", response_model=ListEnvelope[SourceRead])
async def list_sources(
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "admin")),
    service: OsintService = Depends(get_osint_service),
) -> ListEnvelope[SourceRead]:
    items = await service.list_sources(current_user)
    return ListEnvelope(
        data=[SourceRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post("/sources", response_model=Envelope[SourceRead], status_code=status.HTTP_201_CREATED)
async def register_source(
    payload: SourceCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: OsintService = Depends(get_osint_service),
) -> Envelope[SourceRead]:
    source = await service.register_source(payload, current_user, request.state.correlation_id)
    return Envelope(data=SourceRead.model_validate(source), meta=_meta(request))


@router.patch("/sources/{source_id}", response_model=Envelope[SourceRead])
async def update_source(
    source_id: UUID,
    payload: SourceUpdate,
    request: Request,
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("admin")),
    service: OsintService = Depends(get_osint_service),
) -> Envelope[SourceRead]:
    source = await service.update_source(source_id, payload, current_user, if_match)
    return Envelope(data=SourceRead.model_validate(source), meta=_meta(request))


@router.get("/findings", response_model=ListEnvelope[FindingRead])
async def list_findings(
    request: Request,
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: OsintService = Depends(get_osint_service),
) -> ListEnvelope[FindingRead]:
    items = await service.list_findings(current_user, page)
    return ListEnvelope(
        data=[FindingRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=page.limit),
        meta=_meta(request),
    )


@router.get("/findings/{finding_id}", response_model=Envelope[FindingRead])
async def get_finding(
    finding_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: OsintService = Depends(get_osint_service),
) -> Envelope[FindingRead]:
    finding = await service.get_finding(finding_id, current_user)
    return Envelope(data=FindingRead.model_validate(finding), meta=_meta(request))


@router.post("/findings", response_model=Envelope[FindingRead], status_code=status.HTTP_201_CREATED)
async def create_finding(
    payload: FindingCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: OsintService = Depends(get_osint_service),
) -> Envelope[FindingRead]:
    finding = await service.create_finding(payload, current_user, request.state.correlation_id)
    return Envelope(data=FindingRead.model_validate(finding), meta=_meta(request))


@router.post("/findings/{finding_id}/publish", response_model=Envelope[FindingRead])
async def publish_finding(
    finding_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: OsintService = Depends(get_osint_service),
) -> Envelope[FindingRead]:
    finding = await service.publish_finding(finding_id, current_user, request.state.correlation_id)
    return Envelope(data=FindingRead.model_validate(finding), meta=_meta(request))
