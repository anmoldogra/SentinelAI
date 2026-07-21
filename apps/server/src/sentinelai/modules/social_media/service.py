"""social_media business logic (guide Part 5) — account monitoring, content capture,
and publishing into the canonical evidence model. Bodies deferred."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.social_media.models import CapturedContent, SocialAccountObserved
from sentinelai.modules.social_media.repository import SocialMediaUnitOfWork, get_social_media_uow
from sentinelai.modules.social_media.schemas import AccountCreate, ContentCreate
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.pagination import PageParams


class SocialMediaService:
    def __init__(self, uow: SocialMediaUnitOfWork) -> None:
        self._uow = uow

    async def list_accounts(self, actor: CurrentUser) -> Sequence[SocialAccountObserved]:
        raise NotImplementedError

    async def register_account(
        self, data: AccountCreate, actor: CurrentUser, correlation_id: str
    ) -> SocialAccountObserved:
        raise NotImplementedError

    async def list_content(self, actor: CurrentUser, page: PageParams) -> Sequence[CapturedContent]:
        raise NotImplementedError

    async def create_content(
        self, data: ContentCreate, actor: CurrentUser, correlation_id: str
    ) -> CapturedContent:
        raise NotImplementedError

    async def publish_content(
        self, content_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> CapturedContent:
        """Normalize the content into ``ingestion.evidence`` (via ingestion.public)."""
        raise NotImplementedError


def get_social_media_service(
    uow: SocialMediaUnitOfWork = Depends(get_social_media_uow),
) -> SocialMediaService:
    return SocialMediaService(uow)
