"""ingestion HTTP routes — api-design.md §5. Parse and delegate only (guide Part 5)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status

from sentinelai.modules.ingestion.schemas import (
    AttributeSchemaRead,
    ConnectorCreate,
    ConnectorRead,
    ConnectorUpdate,
    CustodyEventCreate,
    CustodyEventRead,
    EvidenceCreate,
    EvidenceRead,
    EvidenceSupersedeCreate,
    UploadReservationRead,
)
from sentinelai.modules.ingestion.service import EvidenceService, get_evidence_service
from sentinelai.platform.auth.dependencies import CurrentUser, require_role
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(prefix="/api/v1", tags=["evidence"])


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


@router.post(
    "/evidence/uploads",
    response_model=Envelope[UploadReservationRead],
    status_code=status.HTTP_201_CREATED,
)
async def reserve_upload(
    payload: dict[str, str],
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[UploadReservationRead]:
    reservation = await service.reserve_upload(
        payload["category"], payload["artifact_type"], current_user
    )
    return Envelope(data=reservation, meta=_meta(request))


@router.post(
    "/evidence", response_model=Envelope[EvidenceRead], status_code=status.HTTP_201_CREATED
)
async def ingest_evidence(
    payload: EvidenceCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[EvidenceRead]:
    evidence = await service.ingest_evidence(payload, current_user, request.state.correlation_id)
    return Envelope(data=EvidenceRead.model_validate(evidence), meta=_meta(request))


@router.post("/evidence/batch", status_code=status.HTTP_207_MULTI_STATUS)
async def ingest_batch(
    payload: list[EvidenceCreate],
    request: Request,
    current_user: CurrentUser = Depends(require_role("system")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[list[dict[str, object]]]:
    results = await service.ingest_batch(payload, current_user, request.state.correlation_id)
    return Envelope(data=results, meta=_meta(request))


@router.get("/evidence", response_model=ListEnvelope[EvidenceRead])
async def list_evidence(
    request: Request,
    category: str | None = Query(None),
    artifact_type: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    text: str | None = Query(None),
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: EvidenceService = Depends(get_evidence_service),
) -> ListEnvelope[EvidenceRead]:
    items, next_cursor, has_more = await service.list_evidence(
        current_user, category, artifact_type, status_filter, text, page
    )
    return ListEnvelope(
        data=[EvidenceRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
        meta=_meta(request),
    )


@router.get("/evidence/{evidence_id}", response_model=Envelope[EvidenceRead])
async def get_evidence(
    evidence_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[EvidenceRead]:
    evidence = await service.get_evidence(evidence_id, current_user)
    return Envelope(data=EvidenceRead.model_validate(evidence), meta=_meta(request))


@router.get("/evidence/{evidence_id}/download")
async def download_evidence(
    evidence_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[dict[str, str]]:
    url = await service.get_download_url(evidence_id, current_user)
    return Envelope(data={"download_url": url}, meta=_meta(request))


@router.get("/evidence/{evidence_id}/custody-events", response_model=ListEnvelope[CustodyEventRead])
async def list_custody_events(
    evidence_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "compliance")),
    service: EvidenceService = Depends(get_evidence_service),
) -> ListEnvelope[CustodyEventRead]:
    items = await service.list_custody_events(evidence_id, current_user)
    return ListEnvelope(
        data=[CustodyEventRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post(
    "/evidence/{evidence_id}/custody-events",
    response_model=Envelope[CustodyEventRead],
    status_code=status.HTTP_201_CREATED,
)
async def record_custody_event(
    evidence_id: UUID,
    payload: CustodyEventCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("supervisor", "admin")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[CustodyEventRead]:
    event = await service.record_custody_event(
        evidence_id, payload, current_user, request.state.correlation_id
    )
    return Envelope(data=CustodyEventRead.model_validate(event), meta=_meta(request))


@router.post("/evidence/{evidence_id}/verify-integrity", response_model=Envelope[EvidenceRead])
async def verify_integrity(
    evidence_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[EvidenceRead]:
    evidence = await service.verify_integrity(evidence_id, current_user)
    return Envelope(data=EvidenceRead.model_validate(evidence), meta=_meta(request))


@router.post(
    "/evidence/{evidence_id}/supersede",
    response_model=Envelope[EvidenceRead],
    status_code=status.HTTP_201_CREATED,
)
async def supersede_evidence(
    evidence_id: UUID,
    payload: EvidenceSupersedeCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[EvidenceRead]:
    evidence = await service.supersede_evidence(
        evidence_id, payload, current_user, request.state.correlation_id
    )
    return Envelope(data=EvidenceRead.model_validate(evidence), meta=_meta(request))


@router.get("/connectors", response_model=ListEnvelope[ConnectorRead])
async def list_connectors(
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: EvidenceService = Depends(get_evidence_service),
) -> ListEnvelope[ConnectorRead]:
    items = await service.list_connectors(current_user)
    return ListEnvelope(
        data=[ConnectorRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post(
    "/connectors", response_model=Envelope[ConnectorRead], status_code=status.HTTP_201_CREATED
)
async def register_connector(
    payload: ConnectorCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[ConnectorRead]:
    connector = await service.register_connector(
        payload, current_user, request.state.correlation_id
    )
    return Envelope(data=ConnectorRead.model_validate(connector), meta=_meta(request))


@router.patch("/connectors/{connector_id}", response_model=Envelope[ConnectorRead])
async def update_connector(
    connector_id: UUID,
    payload: ConnectorUpdate,
    request: Request,
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("admin")),
    service: EvidenceService = Depends(get_evidence_service),
) -> Envelope[ConnectorRead]:
    connector = await service.update_connector(connector_id, payload, current_user, if_match)
    return Envelope(data=ConnectorRead.model_validate(connector), meta=_meta(request))


@router.get("/attribute-schemas", response_model=ListEnvelope[AttributeSchemaRead])
async def list_attribute_schemas(
    request: Request,
    current_user: CurrentUser = Depends(
        require_role("investigator", "admin", "compliance", "supervisor")
    ),
    service: EvidenceService = Depends(get_evidence_service),
) -> ListEnvelope[AttributeSchemaRead]:
    items = await service.list_attribute_schemas(current_user)
    return ListEnvelope(
        data=[AttributeSchemaRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )
