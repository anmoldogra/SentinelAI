"""forensics HTTP routes — api-design.md §4.5. Parse and delegate only."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from sentinelai.modules.forensics.schemas import ArtifactCreate, ArtifactRead
from sentinelai.modules.forensics.service import ForensicsService, get_forensics_service
from sentinelai.platform.auth.dependencies import CurrentUser, require_role
from sentinelai.platform.db.transaction import TransactionalRoute, bind_session
from sentinelai.platform.idempotency import enforce_idempotency
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(
    prefix="/api/v1/forensics",
    tags=["forensics"],
    # ADR-0005 §1: the entrypoint owns the transaction. The route class commits once on
    # success and rolls back on any exception; `bind_session` publishes the request-scoped
    # session for it. Declared here rather than per-handler so no handler can omit it.
    route_class=TransactionalRoute,
    # ADR-0012 / api-design.md §2.9: a mutating request carrying an `Idempotency-Key`
    # replays its stored response instead of re-executing. A no-op without the header, so
    # this changes nothing for the endpoints §2.9 does not cover.
    dependencies=[Depends(bind_session), Depends(enforce_idempotency)],
)


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


@router.get("/artifacts", response_model=ListEnvelope[ArtifactRead])
async def list_artifacts(
    request: Request,
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: ForensicsService = Depends(get_forensics_service),
) -> ListEnvelope[ArtifactRead]:
    items, next_cursor, has_more = await service.list_artifacts(current_user, page)
    return ListEnvelope(
        data=[ArtifactRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
        meta=_meta(request),
    )


@router.post(
    "/artifacts", response_model=Envelope[ArtifactRead], status_code=status.HTTP_201_CREATED
)
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
    artifact = await service.publish_artifact(
        artifact_id, current_user, request.state.correlation_id
    )
    return Envelope(data=ArtifactRead.model_validate(artifact), meta=_meta(request))
