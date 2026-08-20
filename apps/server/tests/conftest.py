"""Shared test fixtures (guide Part 13).

The third-party stack + a live Postgres are required to RUN these; a pure in-memory
``FakeUnitOfWork`` lets the service's business logic be unit-tested without a DB
(Postgres-specific types — JSONB/ARRAY/schemas — rule out SQLite). DB-backed
repository/integration tests belong here too once a test Postgres is provisioned.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from sentinelai.platform.auth.dependencies import CurrentUser


@pytest.fixture
def actor() -> CurrentUser:
    return CurrentUser(user_id=uuid4(), roles=("investigator",))


class _FakeCaseRepo:
    def __init__(self) -> None:
        self.store: dict[UUID, Any] = {}

    async def get_by_id(self, case_id: UUID) -> Any:
        return self.store.get(case_id)

    async def add(self, case: Any) -> None:
        if getattr(case, "case_id", None) is None:  # simulate flush assigning the PK default
            case.case_id = uuid4()
        self.store[case.case_id] = case

    async def list_cases(
        self,
        *,
        owner_id: UUID,
        status: str | None,
        created_after: Any,
        created_before: Any,
        text: str | None,
        limit: int,
        cursor_created_at: Any,
        cursor_case_id: Any,
    ) -> Sequence[Any]:
        rows = [c for c in self.store.values() if c.owning_user_id == owner_id]
        if status is not None:
            rows = [c for c in rows if c.status == status]
        if text:
            rows = [c for c in rows if text.lower() in (c.title or "").lower()]
        rows.sort(key=lambda c: (c.created_at, c.case_id), reverse=True)
        return rows[: limit + 1]


class _FakeLinkRepo:
    def __init__(self) -> None:
        self.store: dict[tuple[UUID, UUID], Any] = {}

    async def add(self, link: Any) -> None:
        self.store[(link.case_id, link.evidence_id)] = link

    async def get(self, case_id: UUID, evidence_id: UUID) -> Any:
        return self.store.get((case_id, evidence_id))

    async def list_for_case(self, case_id: UUID) -> Sequence[Any]:
        return [v for (c, _), v in self.store.items() if c == case_id]

    async def remove(self, link: Any) -> None:
        self.store.pop((link.case_id, link.evidence_id), None)


class _FakeHistoryRepo:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def add(self, entry: Any) -> None:
        self.items.append(entry)

    async def list_for_case(self, case_id: UUID) -> Sequence[Any]:
        return [h for h in self.items if h.case_id == case_id]


class _FakeReportRepo:
    def __init__(self) -> None:
        self.store: dict[UUID, Any] = {}

    async def add(self, report: Any) -> None:
        if getattr(report, "report_id", None) is None:  # mirror the real flush assigning the PK
            report.report_id = uuid4()
        self.store[report.report_id] = report

    async def get_by_id(self, report_id: UUID) -> Any:
        return self.store.get(report_id)

    async def list_for_case(self, case_id: UUID) -> Sequence[Any]:
        return [r for r in self.store.values() if r.case_id == case_id]


class _FakeOutbox:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


class FakeUnitOfWork:
    """Duck-typed stand-in for ``CaseManagementUnitOfWork`` (no DB)."""

    def __init__(self) -> None:
        self.session = SimpleNamespace()
        self.cases = _FakeCaseRepo()
        self.evidence_links = _FakeLinkRepo()
        self.status_history = _FakeHistoryRepo()
        self.reports = _FakeReportRepo()
        self.outbox = _FakeOutbox()
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit writes go through a real DB session; stub them for DB-less unit tests."""

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "sentinelai.modules.case_management.service.record_audit_event", _noop, raising=False
    )
    monkeypatch.setattr(
        "sentinelai.modules.investigation.service.record_audit_event", _noop, raising=False
    )
    monkeypatch.setattr(
        "sentinelai.modules.ingestion.service.record_audit_event", _noop, raising=False
    )


# --- ingestion fakes --------------------------------------------------------
class _FakeEvidenceRepo:
    def __init__(self) -> None:
        self.store: dict[UUID, Any] = {}

    async def get_by_id(self, evidence_id: UUID) -> Any:
        return self.store.get(evidence_id)

    async def add(self, evidence: Any) -> None:
        if getattr(evidence, "evidence_id", None) is None:
            evidence.evidence_id = uuid4()
        self.store[evidence.evidence_id] = evidence

    async def exists(self, evidence_id: UUID) -> bool:
        return evidence_id in self.store

    async def has_replacement(self, evidence_id: UUID) -> bool:
        return any(
            getattr(e, "supersedes_evidence_id", None) == evidence_id for e in self.store.values()
        )

    async def list_(
        self,
        *,
        category,
        artifact_type,
        status,
        text,
        limit,
        cursor_ingested_at,
        cursor_evidence_id,
    ):  # type: ignore[no-untyped-def]
        rows = list(self.store.values())
        if category is not None:
            rows = [e for e in rows if e.category == category]
        if status is not None:
            rows = [e for e in rows if e.status == status]
        rows.sort(key=lambda e: (e.ingested_at, e.evidence_id), reverse=True)
        return rows[: limit + 1]


class _FakeCustodyRepo:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def add(self, event: Any) -> None:
        self.items.append(event)

    async def list_for_evidence(self, evidence_id: UUID) -> Sequence[Any]:
        return sorted(
            [e for e in self.items if e.evidence_id == evidence_id],
            key=lambda e: e.sequence_number,
        )

    async def last_entry(self, evidence_id: UUID) -> Any:
        evs = [e for e in self.items if e.evidence_id == evidence_id]
        return max(evs, key=lambda e: e.sequence_number) if evs else None

    async def last_of_types(self, evidence_id: UUID, event_types: Sequence[str]) -> Any:
        evs = [
            e for e in self.items if e.evidence_id == evidence_id and e.event_type in event_types
        ]
        return max(evs, key=lambda e: e.sequence_number) if evs else None


class _FakeIntakeRepo:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def add(self, record: Any) -> None:
        if getattr(record, "intake_id", None) is None:
            record.intake_id = uuid4()
        self.items.append(record)


class _FakeConnectorRepo:
    def __init__(self) -> None:
        self.store: dict[UUID, Any] = {}

    async def add(self, connector: Any) -> None:
        if getattr(connector, "connector_id", None) is None:
            connector.connector_id = uuid4()
        self.store[connector.connector_id] = connector

    async def get_by_id(self, connector_id: UUID) -> Any:
        return self.store.get(connector_id)

    async def list_(self) -> Sequence[Any]:
        return list(self.store.values())


class _FakeAttrSchemaRepo:
    def __init__(self) -> None:
        self.registered: set[tuple[str, str, str]] = set()

    async def list_(self) -> Sequence[Any]:
        return []

    async def is_registered(self, schema_version: str, category: str, artifact_type: str) -> bool:
        return (schema_version, category, artifact_type) in self.registered


class FakeIngestionUnitOfWork:
    def __init__(self) -> None:
        self.session = SimpleNamespace()
        self.evidence = _FakeEvidenceRepo()
        self.custody = _FakeCustodyRepo()
        self.intake = _FakeIntakeRepo()
        self.connectors = _FakeConnectorRepo()
        self.attribute_schemas = _FakeAttrSchemaRepo()
        self.outbox = _FakeOutbox()
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def ing_uow() -> FakeIngestionUnitOfWork:
    return FakeIngestionUnitOfWork()


# --- investigation fakes ----------------------------------------------------
class _FakeByIdRepo:
    def __init__(self, pk: str) -> None:
        self._pk = pk
        self.store: dict[UUID, Any] = {}

    async def get_by_id(self, key: UUID) -> Any:
        return self.store.get(key)

    async def add(self, obj: Any) -> None:
        if getattr(obj, self._pk, None) is None:
            setattr(obj, self._pk, uuid4())
        self.store[getattr(obj, self._pk)] = obj

    async def list_(
        self, *, status: str | None, limit: int, cursor_id: UUID | None
    ) -> Sequence[Any]:
        rows = list(self.store.values())
        if status is not None:
            rows = [r for r in rows if r.status == status]
        rows.sort(key=lambda r: getattr(r, self._pk))
        if cursor_id is not None:
            rows = [r for r in rows if getattr(r, self._pk) > cursor_id]
        return rows[: limit + 1]


class _FakeRelRepo(_FakeByIdRepo):
    async def list_for_entity(self, entity_id: UUID) -> Sequence[Any]:
        return [
            r
            for r in self.store.values()
            if r.from_entity_id == entity_id or r.to_entity_id == entity_id
        ]


class _FakeListRepo:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def add(self, obj: Any) -> None:
        self.items.append(obj)

    async def list_for_entity(self, entity_id: UUID) -> Sequence[Any]:
        return [i for i in self.items if getattr(i, "entity_id", None) == entity_id]

    async def list_for_relationship(self, relationship_id: UUID) -> Sequence[Any]:
        return [i for i in self.items if getattr(i, "relationship_id", None) == relationship_id]


class FakeInvestigationUnitOfWork:
    def __init__(self) -> None:
        self.session = SimpleNamespace()
        self.entities = _FakeByIdRepo("entity_id")
        self.entity_revisions = _FakeListRepo()
        self.relationships = _FakeRelRepo("relationship_id")
        self.relationship_revisions = _FakeListRepo()
        self.relationship_evidence = _FakeListRepo()
        self.entity_mentions = _FakeListRepo()
        self.correlation_runs = _FakeByIdRepo("run_id")
        self.outbox = _FakeOutbox()
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:  # pragma: no cover
        pass


@pytest.fixture
def inv_uow() -> FakeInvestigationUnitOfWork:
    return FakeInvestigationUnitOfWork()
