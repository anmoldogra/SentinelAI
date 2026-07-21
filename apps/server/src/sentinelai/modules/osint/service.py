"""osint business logic (guide Part 5) — source config, finding capture, and
publishing a finding into the canonical evidence model. Bodies deferred."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.osint.models import OsintFinding, OsintSource
from sentinelai.modules.osint.repository import OsintUnitOfWork, get_osint_uow
from sentinelai.modules.osint.schemas import FindingCreate, SourceCreate, SourceUpdate
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.pagination import PageParams


class OsintService:
    def __init__(self, uow: OsintUnitOfWork) -> None:
        self._uow = uow

    async def list_sources(self, actor: CurrentUser) -> Sequence[OsintSource]:
        raise NotImplementedError

    async def register_source(
        self, data: SourceCreate, actor: CurrentUser, correlation_id: str
    ) -> OsintSource:
        raise NotImplementedError

    async def update_source(
        self, source_id: UUID, data: SourceUpdate, actor: CurrentUser, expected_etag: str
    ) -> OsintSource:
        raise NotImplementedError

    async def list_findings(self, actor: CurrentUser, page: PageParams) -> Sequence[OsintFinding]:
        raise NotImplementedError

    async def get_finding(self, finding_id: UUID, actor: CurrentUser) -> OsintFinding:
        raise NotImplementedError

    async def create_finding(
        self, data: FindingCreate, actor: CurrentUser, correlation_id: str
    ) -> OsintFinding:
        raise NotImplementedError

    async def publish_finding(
        self, finding_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> OsintFinding:
        """Normalize the finding into ``ingestion.evidence`` (via ingestion.public)."""
        raise NotImplementedError


def get_osint_service(uow: OsintUnitOfWork = Depends(get_osint_uow)) -> OsintService:
    return OsintService(uow)
