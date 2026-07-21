"""case_management HTTP routes — api-design.md §7.

Routers parse and delegate only (guide Part 5): resolve auth + DI, call the service,
map the result through a Pydantic schema, wrap in the envelope. No business logic.
Mutations are authorized (RBAC ``require_role`` + ABAC ``require_case_access``); mutable
resources expose an ``ETag`` and ``PATCH`` requires ``If-Match`` (api-design.md §2.6).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from sentinelai.modules.case_management.schemas import (
    CaseCreate,
    CaseEvidenceLinkRead,
    CaseRead,
    CaseReportCreate,
    CaseReportRead,
    CaseStatusHistoryRead,
    CaseStatusUpdate,
    CaseUpdate,
    EvidenceLinkCreate,
)
from sentinelai.modules.case_management.service import (
    CaseSearchFilters,
    CaseService,
    case_etag,
    get_case_service,
)
from sentinelai.platform.auth.dependencies import (
    CurrentUser,
    require_case_access,
    require_role,
)
from sentinelai.platform.tasks import TaskQueue, get_task_queue
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(prefix="/api/v1", tags=["cases"])


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


# --- cases ------------------------------------------------------------------
@router.get("/cases", response_model=ListEnvelope[CaseRead])
async def list_cases(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    created_after: datetime | None = Query(None),
    created_before: datetime | None = Query(None),
    text: str | None = Query(None),
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: CaseService = Depends(get_case_service),
) -> ListEnvelope[CaseRead]:
    filters = CaseSearchFilters(
        status=status_filter,
        created_after=created_after,
        created_before=created_before,
        text=text,
    )
    items, next_cursor, has_more = await service.list_cases(current_user, filters, page)
    return ListEnvelope(
        data=[CaseRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
        meta=_meta(request),
    )


@router.post("/cases", response_model=Envelope[CaseRead], status_code=status.HTTP_201_CREATED)
async def create_case(
    payload: CaseCreate,
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: CaseService = Depends(get_case_service),
) -> Envelope[CaseRead]:
    case = await service.create_case(payload, current_user, request.state.correlation_id)
    response.headers["ETag"] = case_etag(case)
    return Envelope(data=CaseRead.model_validate(case), meta=_meta(request))


@router.get("/cases/{case_id}", response_model=Envelope[CaseRead])
async def get_case(
    case_id: UUID,
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
) -> Envelope[CaseRead]:
    case = await service.get_case(case_id, current_user)
    response.headers["ETag"] = case_etag(case)
    return Envelope(data=CaseRead.model_validate(case), meta=_meta(request))


@router.patch("/cases/{case_id}", response_model=Envelope[CaseRead])
async def update_case(
    case_id: UUID,
    payload: CaseUpdate,
    request: Request,
    response: Response,
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
) -> Envelope[CaseRead]:
    case = await service.update_case(case_id, payload, current_user, if_match)
    response.headers["ETag"] = case_etag(case)
    return Envelope(data=CaseRead.model_validate(case), meta=_meta(request))


@router.post("/cases/{case_id}/status", response_model=Envelope[CaseRead])
async def change_case_status(
    case_id: UUID,
    payload: CaseStatusUpdate,
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
) -> Envelope[CaseRead]:
    case = await service.change_status(case_id, payload, current_user, request.state.correlation_id)
    response.headers["ETag"] = case_etag(case)
    return Envelope(data=CaseRead.model_validate(case), meta=_meta(request))


@router.get("/cases/{case_id}/status-history", response_model=ListEnvelope[CaseStatusHistoryRead])
async def list_status_history(
    case_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
) -> ListEnvelope[CaseStatusHistoryRead]:
    items = await service.list_status_history(case_id, current_user)
    return ListEnvelope(
        data=[CaseStatusHistoryRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


# --- case ↔ evidence links --------------------------------------------------
@router.get("/cases/{case_id}/evidence", response_model=ListEnvelope[CaseEvidenceLinkRead])
async def list_case_evidence(
    case_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
) -> ListEnvelope[CaseEvidenceLinkRead]:
    items = await service.list_case_evidence(case_id, current_user)
    return ListEnvelope(
        data=[CaseEvidenceLinkRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post(
    "/cases/{case_id}/evidence",
    response_model=Envelope[CaseEvidenceLinkRead],
    status_code=status.HTTP_201_CREATED,
)
async def link_evidence(
    case_id: UUID,
    payload: EvidenceLinkCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
) -> Envelope[CaseEvidenceLinkRead]:
    link = await service.link_evidence(case_id, payload, current_user, request.state.correlation_id)
    return Envelope(data=CaseEvidenceLinkRead.model_validate(link), meta=_meta(request))


@router.delete("/cases/{case_id}/evidence/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_evidence(
    case_id: UUID,
    evidence_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
) -> Response:
    await service.unlink_evidence(case_id, evidence_id, current_user, request.state.correlation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- reports ----------------------------------------------------------------
@router.get("/cases/{case_id}/reports", response_model=ListEnvelope[CaseReportRead])
async def list_reports(
    case_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
) -> ListEnvelope[CaseReportRead]:
    items = await service.list_reports(case_id, current_user)
    return ListEnvelope(
        data=[CaseReportRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post("/cases/{case_id}/reports", status_code=status.HTTP_202_ACCEPTED)
async def generate_report(
    case_id: UUID,
    payload: CaseReportCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_case_access()),
    service: CaseService = Depends(get_case_service),
    task_queue: TaskQueue = Depends(get_task_queue),
) -> Envelope[dict[str, str]]:
    job_id = await service.generate_report(
        case_id, payload, current_user, request.state.correlation_id, task_queue
    )
    return Envelope(data={"job_id": job_id, "status": "accepted"}, meta=_meta(request))


@router.get("/reports/{report_id}", response_model=Envelope[CaseReportRead])
async def get_report(
    report_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: CaseService = Depends(get_case_service),
) -> Envelope[CaseReportRead]:
    report = await service.get_report(report_id, current_user)
    return Envelope(data=CaseReportRead.model_validate(report), meta=_meta(request))


@router.get("/reports/{report_id}/download")
async def download_report(
    report_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: CaseService = Depends(get_case_service),
) -> Envelope[dict[str, str]]:
    url = await service.get_report_download_url(report_id, current_user)
    return Envelope(data={"download_url": url}, meta=_meta(request))
