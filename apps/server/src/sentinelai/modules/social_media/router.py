"""social_media HTTP routes — api-design.md §5 (Social Media). Parse and delegate only."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from sentinelai.modules.social_media.schemas import (
    AccountCreate,
    AccountRead,
    ContentCreate,
    ContentRead,
)
from sentinelai.modules.social_media.service import SocialMediaService, get_social_media_service
from sentinelai.platform.auth.dependencies import CurrentUser, require_role
from sentinelai.shared.envelope import Envelope, ListEnvelope, Meta, Pagination
from sentinelai.shared.pagination import PageParams, page_params

router = APIRouter(prefix="/api/v1/social-media", tags=["social-media"])


def _meta(request: Request) -> Meta:
    return Meta(request_id=request.state.request_id, correlation_id=request.state.correlation_id)


@router.get("/accounts", response_model=ListEnvelope[AccountRead])
async def list_accounts(
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: SocialMediaService = Depends(get_social_media_service),
) -> ListEnvelope[AccountRead]:
    items = await service.list_accounts(current_user)
    return ListEnvelope(
        data=[AccountRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=len(items)),
        meta=_meta(request),
    )


@router.post("/accounts", response_model=Envelope[AccountRead], status_code=status.HTTP_201_CREATED)
async def register_account(
    payload: AccountCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "admin")),
    service: SocialMediaService = Depends(get_social_media_service),
) -> Envelope[AccountRead]:
    account = await service.register_account(payload, current_user, request.state.correlation_id)
    return Envelope(data=AccountRead.model_validate(account), meta=_meta(request))


@router.get("/content", response_model=ListEnvelope[ContentRead])
async def list_content(
    request: Request,
    page: PageParams = Depends(page_params),
    current_user: CurrentUser = Depends(require_role("investigator")),
    service: SocialMediaService = Depends(get_social_media_service),
) -> ListEnvelope[ContentRead]:
    items = await service.list_content(current_user, page)
    return ListEnvelope(
        data=[ContentRead.model_validate(i) for i in items],
        pagination=Pagination(next_cursor=None, has_more=False, limit=page.limit),
        meta=_meta(request),
    )


@router.post("/content", response_model=Envelope[ContentRead], status_code=status.HTTP_201_CREATED)
async def create_content(
    payload: ContentCreate,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: SocialMediaService = Depends(get_social_media_service),
) -> Envelope[ContentRead]:
    content = await service.create_content(payload, current_user, request.state.correlation_id)
    return Envelope(data=ContentRead.model_validate(content), meta=_meta(request))


@router.post("/content/{content_id}/publish", response_model=Envelope[ContentRead])
async def publish_content(
    content_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(require_role("investigator", "system")),
    service: SocialMediaService = Depends(get_social_media_service),
) -> Envelope[ContentRead]:
    content = await service.publish_content(content_id, current_user, request.state.correlation_id)
    return Envelope(data=ContentRead.model_validate(content), meta=_meta(request))
