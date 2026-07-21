"""notification HTTP routes — api-design.md §8. Parse and delegate only."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status

from sentinelai.modules.notification.schemas import (
    NotificationRead,
    NotificationRuleCreate,
    NotificationRuleRead,
    NotificationRuleUpdate,
)
from sentinelai.modules.notification.service import NotificationService, get_notification_service
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user, require_role
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(prefix="/api/v1", tags=["notification"])


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


@router.get("/notifications", response_model=ListEnvelope[NotificationRead])
async def list_notifications(
    request: Request,
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
) -> ListEnvelope[NotificationRead]:
    items = await service.list_notifications(current_user, page)
    return ListEnvelope(
        data=[NotificationRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=page.limit),
        meta=_meta(request),
    )


@router.patch("/notifications/{notification_id}/read", response_model=Envelope[NotificationRead])
async def mark_notification_read(
    notification_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    service: NotificationService = Depends(get_notification_service),
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
    await service.redeliver(notification_id, current_user, request.state.correlation_id)
    return Envelope(data={"status": "accepted"}, meta=_meta(request))


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
    if_match: str = Header(..., alias="If-Match"),
    current_user: CurrentUser = Depends(require_role("admin")),
    service: NotificationService = Depends(get_notification_service),
) -> Envelope[NotificationRuleRead]:
    rule = await service.update_rule(rule_id, payload, current_user, if_match)
    return Envelope(data=NotificationRuleRead.model_validate(rule), meta=_meta(request))
