"""forensics business logic (guide Part 5) — artifact intake, parsing/normalization,
and publishing into the canonical evidence model. Bodies deferred."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.forensics.models import Artifact
from sentinelai.modules.forensics.repository import ForensicsUnitOfWork, get_forensics_uow
from sentinelai.modules.forensics.schemas import ArtifactCreate
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.pagination import PageParams


class ForensicsService:
    def __init__(self, uow: ForensicsUnitOfWork) -> None:
        self._uow = uow

    async def list_artifacts(self, actor: CurrentUser, page: PageParams) -> Sequence[Artifact]:
        raise NotImplementedError

    async def get_artifact(self, artifact_id: UUID, actor: CurrentUser) -> Artifact:
        raise NotImplementedError

    async def register_artifact(
        self, data: ArtifactCreate, actor: CurrentUser, correlation_id: str
    ) -> Artifact:
        raise NotImplementedError

    async def publish_artifact(
        self, artifact_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> Artifact:
        """Normalize the artifact into ``ingestion.evidence`` (via ingestion.public)."""
        raise NotImplementedError


def get_forensics_service(
    uow: ForensicsUnitOfWork = Depends(get_forensics_uow),
) -> ForensicsService:
    return ForensicsService(uow)
