"""investigation HTTP routes — api-design.md §6. Parse and delegate only.

The finding-review endpoints (``PATCH /entities|relationships/{id}/status``) are the
API surface of the human-in-the-loop guarantee (PRD FR-7.3): a disposition only
changes on an explicit analyst action, guarded by ``If-Match`` optimistic concurrency.
The AI-findings review queue is ``GET /relationships?status=proposed`` (database-design §7).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from sentinelai.modules.investigation.models import STATUS_CONFIRMED, STATUS_PROPOSED
from sentinelai.modules.investigation.read import MAX_DEPTH
from sentinelai.modules.investigation.repository import (
    InvestigationUnitOfWork,
    get_investigation_uow,
)
from sentinelai.modules.investigation.schemas import (
    CorrelationRunRead,
    EntityCreate,
    EntityMentionRead,
    EntityRead,
    EntityStatusUpdate,
    GraphEdgeRead,
    GraphNodeRead,
    GraphRead,
    RelationshipEvidenceRead,
    RelationshipRead,
    RelationshipStatusUpdate,
)
from sentinelai.modules.investigation.service import (
    InvestigationService,
    entity_etag,
    get_investigation_service,
    relationship_etag,
)
from sentinelai.platform.auth.dependencies import CurrentUser, require_case_access, require_role
from sentinelai.platform.db.transaction import TransactionalRoute, bind_session
from sentinelai.platform.idempotency import enforce_idempotency
from sentinelai.platform.tasks import TaskQueue, get_task_queue
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(
    prefix="/api/v1",
    tags=["investigation"],
    # ADR-0005 §1: the entrypoint owns the transaction. The route class commits once on
    # success and rolls back on any exception; `bind_session` publishes the request-scoped
    # session for it. Declared here rather than per-handler so no handler can omit it.
    route_class=TransactionalRoute,
    # ADR-0012 / api-design.md §2.9: a mutating request carrying an `Idempotency-Key`
    # replays its stored response instead of re-executing. A no-op without the header, so
    # this changes nothing for the endpoints §2.9 does not cover.
    dependencies=[Depends(bind_session), Depends(enforce_idempotency)],
)


def _split_csv(value: str | None) -> list[str] | None:
    """Parse a comma-separated filter, dropping blanks.

    ``None`` means "no filter"; an empty or all-blank string means the same, rather than a filter
    that matches nothing — a client sending ``?entity_types=`` means it did not filter, and an
    empty subgraph would be a confusing answer to that.
    """
    if value is None:
        return None
    parts = [item.strip() for item in value.split(",") if item.strip()]
    return parts or None


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


# --- entities ---------------------------------------------------------------
@router.get("/entities", response_model=ListEnvelope[EntityRead])
async def list_entities(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
) -> ListEnvelope[EntityRead]:
    items, next_cursor, has_more = await service.list_entities(current_user, status_filter, page)
    return ListEnvelope(
        data=[EntityRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
        meta=_meta(request),
    )


@router.post("/entities", response_model=Envelope[EntityRead], status_code=status.HTTP_201_CREATED)
async def create_entity(
    payload: EntityCreate,
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
    uow: InvestigationUnitOfWork = Depends(get_investigation_uow),
) -> Envelope[EntityRead]:
    entity = await service.create_entity(payload, current_user, request.state.correlation_id)
    response.headers["ETag"] = entity_etag(entity)
    return Envelope(data=EntityRead.model_validate(entity), meta=_meta(request))


@router.get("/entities/{entity_id}", response_model=Envelope[EntityRead])
async def get_entity(
    entity_id: UUID,
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
) -> Envelope[EntityRead]:
    entity = await service.get_entity(entity_id, current_user)
    response.headers["ETag"] = entity_etag(entity)
    return Envelope(data=EntityRead.model_validate(entity), meta=_meta(request))


@router.patch("/entities/{entity_id}/status", response_model=Envelope[EntityRead])
async def review_entity_status(
    entity_id: UUID,
    payload: EntityStatusUpdate,
    request: Request,
    response: Response,
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
    uow: InvestigationUnitOfWork = Depends(get_investigation_uow),
) -> Envelope[EntityRead]:
    entity = await service.review_entity_status(
        entity_id, payload.status, current_user, request.state.correlation_id, if_match
    )
    response.headers["ETag"] = entity_etag(entity)
    return Envelope(data=EntityRead.model_validate(entity), meta=_meta(request))


@router.get("/entities/{entity_id}/relationships", response_model=ListEnvelope[RelationshipRead])
async def list_entity_relationships(
    entity_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
) -> ListEnvelope[RelationshipRead]:
    items = await service.list_entity_relationships(entity_id, current_user)
    return ListEnvelope(
        data=[RelationshipRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.get("/entities/{entity_id}/evidence", response_model=ListEnvelope[EntityMentionRead])
async def list_entity_evidence(
    entity_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
) -> ListEnvelope[EntityMentionRead]:
    items = await service.list_entity_evidence(entity_id, current_user)
    return ListEnvelope(
        data=[EntityMentionRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


# --- relationships ----------------------------------------------------------
@router.get("/relationships", response_model=ListEnvelope[RelationshipRead])
async def list_relationships(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
) -> ListEnvelope[RelationshipRead]:
    items, next_cursor, has_more = await service.list_relationships(
        current_user, status_filter, page
    )
    return ListEnvelope(
        data=[RelationshipRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
        meta=_meta(request),
    )


@router.get("/relationships/{relationship_id}", response_model=Envelope[RelationshipRead])
async def get_relationship(
    relationship_id: UUID,
    request: Request,
    response: Response,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
) -> Envelope[RelationshipRead]:
    relationship = await service.get_relationship(relationship_id, current_user)
    response.headers["ETag"] = relationship_etag(relationship)
    return Envelope(data=RelationshipRead.model_validate(relationship), meta=_meta(request))


@router.patch("/relationships/{relationship_id}/status", response_model=Envelope[RelationshipRead])
async def review_relationship_status(
    relationship_id: UUID,
    payload: RelationshipStatusUpdate,
    request: Request,
    response: Response,
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
    uow: InvestigationUnitOfWork = Depends(get_investigation_uow),
) -> Envelope[RelationshipRead]:
    relationship = await service.review_relationship_status(
        relationship_id,
        payload.status,
        payload.note,
        current_user,
        request.state.correlation_id,
        if_match,
    )
    response.headers["ETag"] = relationship_etag(relationship)
    return Envelope(data=RelationshipRead.model_validate(relationship), meta=_meta(request))


@router.get(
    "/relationships/{relationship_id}/evidence",
    response_model=ListEnvelope[RelationshipEvidenceRead],
)
async def list_relationship_evidence(
    relationship_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
) -> ListEnvelope[RelationshipEvidenceRead]:
    items = await service.list_relationship_evidence(relationship_id, current_user)
    return ListEnvelope(
        data=[RelationshipEvidenceRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


# --- correlation runs & graph ----------------------------------------------
@router.post(
    "/cases/{case_id}/correlation-runs",
    response_model=Envelope[CorrelationRunRead],
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_correlation_run(
    case_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_case_access()),
    service: InvestigationService = Depends(get_investigation_service),
    task_queue: TaskQueue = Depends(get_task_queue),
    uow: InvestigationUnitOfWork = Depends(get_investigation_uow),
) -> Envelope[CorrelationRunRead]:
    run = await service.trigger_correlation_run(
        case_id, current_user, request.state.correlation_id, task_queue
    )
    return Envelope(data=CorrelationRunRead.model_validate(run), meta=_meta(request))


@router.get("/correlation-runs/{run_id}", response_model=Envelope[CorrelationRunRead])
async def get_correlation_run(
    run_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: InvestigationService = Depends(get_investigation_service),
) -> Envelope[CorrelationRunRead]:
    run = await service.get_correlation_run(run_id, current_user)
    return Envelope(data=CorrelationRunRead.model_validate(run), meta=_meta(request))


@router.get("/cases/{case_id}/graph", response_model=Envelope[GraphRead])
async def get_case_graph(
    case_id: UUID,
    request: Request,
    status: str = Query(
        default=f"{STATUS_PROPOSED},{STATUS_CONFIRMED}",
        description="Comma-separated dispositions to include.",
    ),
    entity_types: str | None = Query(default=None, description="Comma-separated entity types."),
    min_confidence: Decimal | None = Query(default=None, ge=0, le=1),
    depth: int = Query(default=1, ge=0, le=MAX_DEPTH),
    current_user: CurrentUser = Depends(require_case_access()),
    service: InvestigationService = Depends(get_investigation_service),
) -> Envelope[GraphRead]:
    """Return the case's entity/relationship subgraph (api-design.md §6, ADR-0013).

    Served entirely from the CQRS projection — this route touches no transactional table.

    ``require_case_access`` is the authorization, and it is the same owner-or-member ABAC check
    every
    other case-scoped route uses (ADR-0017). The projection deliberately holds no access rules of
    its
    own: a read model that made authorization decisions would be a second place for them to drift
    from `platform`'s, and it would be the place nobody thinks to audit.

    ``depth`` is capped at 3 by §6 ("to bound query cost") and the cap is enforced twice — here, and
    again in the traversal, so a caller reaching the repository another way cannot ask for an
    unbounded walk.
    """
    nodes, edges = await service.get_case_graph(
        case_id,
        current_user,
        statuses=_split_csv(status),
        entity_types=_split_csv(entity_types),
        min_confidence=min_confidence,
        depth=depth,
    )
    graph = GraphRead(
        entities=[GraphNodeRead.model_validate(n) for n in nodes],
        relationships=[GraphEdgeRead.model_validate(e) for e in edges],
    )
    return Envelope(data=graph, meta=_meta(request))
