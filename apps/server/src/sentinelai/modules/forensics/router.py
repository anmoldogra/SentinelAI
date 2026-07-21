"""forensics HTTP routes — api-design.md §5 (Forensics). Parse and delegate only."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from sentinelai.modules.forensics.schemas import ArtifactCreate, ArtifactRead
from sentinelai.modules.forensics.service import ForensicsService, get_forensics_service
from sentinelai.platform.auth.dependencies import CurrentUser, require_role
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(prefix="/api/v1/forensics", tags=["forensics"])


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


@router.get("/artifacts", response_model=ListEnvelope[ArtifactRead])
async def list_artifacts(
    request: Request,
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: ForensicsService = Depends(get_forensics_service),
) -> ListEnvelope[ArtifactRead]:
    items = await service.list_artifacts(current_user, page)
    return ListEnvelope(
        data=[ArtifactRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=page.limit),
        meta=_meta(request),
    )


@router.post("/artifacts", response_model=Envelope[ArtifactRead], status_code=status.HTTP_201_CREATED)
async def register_artifact(
    payload: ArtifactCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: ForensicsService = Depends(get_forensics_service),
) -> Envelope[ArtifactRead]:
    artifact = await service.register_artifact(payload, current_user, request.state.correlation_id)
    return Envelope(data=ArtifactRead.model_validate(artifact), meta=_meta(request))


@router.get("/artifacts/{artifact_id}", response_model=Envelope[ArtifactRead])
async def get_artifact(
    artifact_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: ForensicsService = Depends(get_forensics_service),
) -> Envelope[ArtifactRead]:
    artifact = await service.get_artifact(artifact_id, current_user)
    return Envelope(data=ArtifactRead.model_validate(artifact), meta=_meta(request))


@router.post("/artifacts/{artifact_id}/publish", response_model=Envelope[ArtifactRead])
async def publish_artifact(
    artifact_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: ForensicsService = Depends(get_forensics_service),
) -> Envelope[ArtifactRead]:
    artifact = await service.publish_artifact(artifact_id, current_user, request.state.correlation_id)
    return Envelope(data=ArtifactRead.model_validate(artifact), meta=_meta(request))
