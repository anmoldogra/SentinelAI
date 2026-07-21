"""ingestion business logic (guide Part 5) — CEM §13 validation, custody chaining,
supersession, integrity verification. Bodies deferred (``NotImplementedError``).
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from fastapi import Depends

from sentinelai.modules.ingestion.models import (
    AttributeSchemaRegistry,
    ConnectorRegistry,
    Evidence,
    EvidenceCustodyEvent,
)
from sentinelai.modules.ingestion.repository import IngestionUnitOfWork, get_ingestion_uow
from sentinelai.modules.ingestion.schemas import (
    ConnectorCreate,
    ConnectorUpdate,
    CustodyEventCreate,
    EvidenceCreate,
    EvidenceSupersedeCreate,
    UploadReservationRead,
)
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.pagination import PageParams


class EvidenceService:
    """Intake, canonicalization, custody, and integrity for evidence."""

    def __init__(self, uow: IngestionUnitOfWork) -> None:
        self._uow = uow

    async def reserve_upload(
        self, category: str, artifact_type: str, actor: CurrentUser
    ) -> UploadReservationRead:
        raise NotImplementedError

    async def ingest_evidence(
        self, data: EvidenceCreate, actor: CurrentUser, correlation_id: str
    ) -> Evidence:
        raise NotImplementedError

    async def ingest_batch(
        self, items: Sequence[EvidenceCreate], actor: CurrentUser, correlation_id: str
    ) -> list[dict[str, object]]:
        raise NotImplementedError

    async def get_evidence(self, evidence_id: UUID, actor: CurrentUser) -> Evidence:
        raise NotImplementedError

    async def list_evidence(self, actor: CurrentUser, page: PageParams) -> Sequence[Evidence]:
        raise NotImplementedError

    async def get_download_url(self, evidence_id: UUID, actor: CurrentUser) -> str:
        raise NotImplementedError

    async def list_custody_events(
        self, evidence_id: UUID, actor: CurrentUser
    ) -> Sequence[EvidenceCustodyEvent]:
        raise NotImplementedError

    async def record_custody_event(
        self, evidence_id: UUID, data: CustodyEventCreate, actor: CurrentUser, correlation_id: str
    ) -> EvidenceCustodyEvent:
        raise NotImplementedError

    async def verify_integrity(self, evidence_id: UUID, actor: CurrentUser) -> Evidence:
        raise NotImplementedError

    async def supersede_evidence(
        self, evidence_id: UUID, data: EvidenceSupersedeCreate, actor: CurrentUser, correlation_id: str
    ) -> Evidence:
        raise NotImplementedError

    async def exists(self, evidence_id: UUID) -> bool:
        """Cross-module hook (ingestion.public) — used by case_management at link time."""
        raise NotImplementedError

    async def list_connectors(self, actor: CurrentUser) -> Sequence[ConnectorRegistry]:
        raise NotImplementedError

    async def register_connector(
        self, data: ConnectorCreate, actor: CurrentUser, correlation_id: str
    ) -> ConnectorRegistry:
        raise NotImplementedError

    async def update_connector(
        self, connector_id: UUID, data: ConnectorUpdate, actor: CurrentUser, expected_etag: str
    ) -> ConnectorRegistry:
        raise NotImplementedError

    async def list_attribute_schemas(self, actor: CurrentUser) -> Sequence[AttributeSchemaRegistry]:
        raise NotImplementedError


def get_evidence_service(
    uow: IngestionUnitOfWork = Depends(get_ingestion_uow),
) -> EvidenceService:
    return EvidenceService(uow)
