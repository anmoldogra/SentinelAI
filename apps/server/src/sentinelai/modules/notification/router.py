"""notification HTTP routes — api-design.md §8. Parse and delegate only.

The entrypoint owns the transaction (ADR-0005): mutating endpoints commit the request-scoped
UnitOfWork once after the service returns — the SAME instance the service was built on, via
FastAPI's per-request dependency cache.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from sentinelai.modules.notification.repository import (
    NotificationUnitOfWork,
    get_notification_uow,
)
from sentinelai.modules.notification.schemas import (
    NotificationRead,
    NotificationRuleCreate,
    NotificationRuleRead,
    NotificationRuleUpdate,
)
from sentinelai.modules.notification.service import (
    NotificationService,
    get_notification_service,
    rule_etag,
)
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user, require_role
from sentinelai.platform.db.transaction import TransactionalRoute, bind_session
from sentinelai.platform.idempotency import enforce_idempotency
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(
    prefix="/api/v1",
    tags=["notification"],
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


@router.get("/notifications", response_model=ListEnvelope[NotificationRead])
async def list_notifications(
    request: Request,
    read: bool | None = Query(default=None, description="Filter by read state."),
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> ListEnvelope[NotificationRead]:
    """The caller's own notifications, newest first (§8).

    ``read`` is §8's documented filter; omitted means both. The recipient is never a parameter — it
    is always the authenticated caller.
    """
    items, next_cursor, has_more = await service.list_notifications(current_user, page, read=read)
    return ListEnvelope(
        data=[NotificationRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=next_cursor, has_more=has_more, limit=page.limit),
        meta=_meta(request),
    )


@router.patch("/notifications/{notification_id}/read", response_model=Envelope[NotificationRead])
async def mark_notification_read(
    notification_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
    uow: NotificationUnitOfWork = Depends(get_notification_uow),
) -> Envelope[NotificationRead]:
    notification = await service.mark_read(notification_id, current_user)
    return Envelope(data=NotificationRead.model_validate(notification), meta=_meta(request))


@router.post("/notifications/{notification_id}/redeliver", status_code=status.HTTP_202_ACCEPTED)
async def redeliver_notification(
    notification_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: NotificationService = Depends(get_notification_service),
) -> Envelope[dict[str, str]]:
    """Retry a failed delivery (§4.9). Admin-only, audited.

    `202` with the attempt's real outcome rather than a bare "accepted": the sender is in-process
    and §4.9 documents no job, so by the time this responds the attempt has been made and recorded.
    Saying
    only "accepted" would hide a second failure from the operator who just retried it.
    """
    delivery = await service.redeliver(notification_id, current_user, request.state.correlation_id)
    return Envelope(
        data={"status": "accepted", "delivery_status": delivery.delivery_status},
        meta=_meta(request),
    )


@router.get("/notification-rules", response_model=ListEnvelope[NotificationRuleRead])
async def list_rules(
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: NotificationService = Depends(get_notification_service),
) -> ListEnvelope[NotificationRuleRead]:
    items = await service.list_rules(current_user)
    return ListEnvelope(
        data=[NotificationRuleRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post(
    "/notification-rules",
    response_model=Envelope[NotificationRuleRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_rule(
    payload: NotificationRuleCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("admin")),
    service: NotificationService = Depends(get_notification_service),
) -> Envelope[NotificationRuleRead]:
    rule = await service.create_rule(payload, current_user, request.state.correlation_id)
    return Envelope(data=NotificationRuleRead.model_validate(rule), meta=_meta(request))


@router.patch("/notification-rules/{rule_id}", response_model=Envelope[NotificationRuleRead])
async def update_rule(
    rule_id: UUID,
    payload: NotificationRuleUpdate,
    request: Request,
    response: Response,
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("admin")),
    service: NotificationService = Depends(get_notification_service),
) -> Envelope[NotificationRuleRead]:
    """Update or deactivate a rule (§4.9), guarded by ``If-Match`` (§2.6).

    The response carries the **new** ``ETag`` so a client can make a second change without
    re-reading. §2.6 asks for an ETag on every GET of a mutable resource, and §8 documents no
    single-rule GET — so without this a client has no in-contract way to obtain the value a later
    `If-Match` needs. Noted
    as a contract gap in the implementation log rather than closed by inventing an endpoint;
    `osint`'s source CRUD has the same hole.
    """
    rule = await service.update_rule(rule_id, payload, current_user, if_match)
    response.headers["ETag"] = rule_etag(rule)
    return Envelope(data=NotificationRuleRead.model_validate(rule), meta=_meta(request))
