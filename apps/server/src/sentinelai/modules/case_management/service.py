"""case_management business logic (guide Part 5) — real implementation.

All business rules live here: the case lifecycle state machine, optimistic-concurrency
ETag checks, evidence-link dedup, and report orchestration. Every mutation writes its
outbox event in the SAME transaction as the business write (event-driven §16) and an
audit entry (security §22), then commits once. Reads enforce ownership (defense in
depth behind the HTTP ABAC guard).

Documented lifecycle (database-design.md §8 / api-design.md): ``open | closed |
archived``, initial ``open``. Transition edges (``archived`` terminal, ``open↔closed``
reversible) are inferred — the states are documented, the exact edges are not.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.case_management.events import (
    EVENT_CASE_CREATED,
    EVENT_CASE_STATUS_CHANGED,
    EVENT_EVIDENCE_LINKED_TO_CASE,
    EVENT_EVIDENCE_UNLINKED_FROM_CASE,
)
from sentinelai.modules.case_management.exceptions import (
    CaseNotFoundError,
    EvidenceAlreadyLinkedError,
    EvidenceLinkNotFoundError,
    InvalidCaseStatusTransitionError,
    ReportNotFoundError,
)
from sentinelai.modules.case_management.models import (
    Case,
    CaseEvidenceLink,
    CaseReport,
    CaseStatusHistory,
)
from sentinelai.modules.case_management.repository import (
    CaseManagementUnitOfWork,
    get_case_management_uow,
)
from sentinelai.modules.case_management.schemas import (
    CaseCreate,
    CaseReportCreate,
    CaseStatusUpdate,
    CaseUpdate,
    EvidenceLinkCreate,
)
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CaseAccessChecker, CurrentUser
from sentinelai.platform.db.session import get_session
from sentinelai.platform.tasks import TaskQueue, get_task_queue
from sentinelai.shared.exceptions import (
    ForbiddenError,
    PreconditionFailedError,
    ValidationFailedError,
)
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_ARCHIVED = "archived"
VALID_STATUSES = frozenset({STATUS_OPEN, STATUS_CLOSED, STATUS_ARCHIVED})
# Allowed transitions (see module docstring). archived is terminal.
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_OPEN: frozenset({STATUS_CLOSED, STATUS_ARCHIVED}),
    STATUS_CLOSED: frozenset({STATUS_OPEN, STATUS_ARCHIVED}),
    STATUS_ARCHIVED: frozenset(),
}


def case_etag(case: Case) -> str:
    """Weak, content-derived ETag over a case's mutable fields (api-design.md §2.6)."""
    digest = hashlib.sha256(
        json.dumps(
            {"title": case.title, "description": case.description, "status": case.status},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f'W/"{digest}"'


@dataclass(frozen=True, slots=True)
class CaseSearchFilters:
    """Relational filters for GET /cases (documented columns only)."""

    status: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    text: str | None = None


def _actor_role(actor: CurrentUser) -> str:
    return actor.roles[0] if actor.roles else "unknown"


class CaseService:
    """Case lifecycle, evidence linking, status history, and report orchestration."""

    def __init__(self, uow: CaseManagementUnitOfWork) -> None:
        self._uow = uow

    # -- internal helpers ---------------------------------------------------
    async def _load_owned(self, case_id: UUID, actor: CurrentUser) -> Case:
        """Load a case the actor may access, or raise. Access = ownership (Phase 1)."""
        case = await self._uow.cases.get_by_id(case_id)
        if case is None:
            raise CaseNotFoundError()
        if case.owning_user_id != actor.user_id:
            raise ForbiddenError()
        return case

    async def _audit(
        self, actor: CurrentUser, action: str, target_id: UUID, details: dict[str, object]
    ) -> None:
        await record_audit_event(
            self._uow.session,
            actor_user_id=actor.user_id,
            actor_role=_actor_role(actor),
            action=action,
            module="case_management",
            target_type="case",
            target_id=target_id,
            details=details,
        )

    async def _apply_transition(
        self,
        case: Case,
        new_status: str,
        notes: str | None,
        actor: CurrentUser,
        correlation_id: str,
    ) -> None:
        if new_status not in VALID_STATUSES:
            raise ValidationFailedError(
                [{"field": "new_status", "message": f"unknown status '{new_status}'"}]
            )
        if new_status not in _TRANSITIONS[case.status]:
            raise InvalidCaseStatusTransitionError(
                f"cannot transition case from '{case.status}' to '{new_status}'"
            )
        previous = case.status
        now = datetime.now(UTC)
        case.status = new_status
        if new_status == STATUS_CLOSED:
            case.closed_at = now
        elif new_status == STATUS_OPEN:
            case.closed_at = None

        await self._uow.status_history.add(
            CaseStatusHistory(
                case_id=case.case_id,
                previous_status=previous,
                new_status=new_status,
                actor_user_id=actor.user_id,
                changed_at=now,
                notes=notes,
            )
        )
        await self._uow.outbox.publish(
            event_type=EVENT_CASE_STATUS_CHANGED,
            aggregate_type="case",
            aggregate_id=case.case_id,
            payload={
                "case_id": str(case.case_id),
                "previous_status": previous,
                "new_status": new_status,
            },
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor,
            EVENT_CASE_STATUS_CHANGED,
            case.case_id,
            {"previous_status": previous, "new_status": new_status},
        )

    # -- commands -----------------------------------------------------------
    async def create_case(self, data: CaseCreate, actor: CurrentUser, correlation_id: str) -> Case:
        case = Case(
            title=data.title,
            description=data.description,
            status=STATUS_OPEN,
            owning_user_id=actor.user_id,
            created_at=datetime.now(UTC),
            closed_at=None,
        )
        await self._uow.cases.add(case)
        await self._uow.outbox.publish(
            event_type=EVENT_CASE_CREATED,
            aggregate_type="case",
            aggregate_id=case.case_id,
            payload={"case_id": str(case.case_id), "owning_user_id": str(case.owning_user_id)},
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(actor, EVENT_CASE_CREATED, case.case_id, {"title": case.title})
        await self._uow.commit()
        return case

    async def update_case(
        self, case_id: UUID, data: CaseUpdate, actor: CurrentUser, expected_etag: str
    ) -> Case:
        case = await self._load_owned(case_id, actor)
        if _normalize_etag(expected_etag) != _normalize_etag(case_etag(case)):
            raise PreconditionFailedError("case was modified by someone else (ETag mismatch)")
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(case, field, value)
        await self._uow.cases.add(case)  # flush the update
        # No `case.updated` event exists in the catalog (§25.7) — audit only.
        await self._audit(actor, "case.updated", case.case_id, {"fields": sorted(changes)})
        await self._uow.commit()
        return case

    async def change_status(
        self, case_id: UUID, data: CaseStatusUpdate, actor: CurrentUser, correlation_id: str
    ) -> Case:
        case = await self._load_owned(case_id, actor)
        await self._apply_transition(case, data.new_status, data.notes, actor, correlation_id)
        await self._uow.commit()
        return case

    async def close_case(self, case_id: UUID, actor: CurrentUser, correlation_id: str) -> Case:
        """Convenience transition to ``closed`` (no dedicated endpoint — POST /status)."""
        case = await self._load_owned(case_id, actor)
        await self._apply_transition(case, STATUS_CLOSED, None, actor, correlation_id)
        await self._uow.commit()
        return case

    async def reopen_case(self, case_id: UUID, actor: CurrentUser, correlation_id: str) -> Case:
        """Convenience transition back to ``open`` (only valid from ``closed``)."""
        case = await self._load_owned(case_id, actor)
        await self._apply_transition(case, STATUS_OPEN, None, actor, correlation_id)
        await self._uow.commit()
        return case

    async def link_evidence(
        self, case_id: UUID, data: EvidenceLinkCreate, actor: CurrentUser, correlation_id: str
    ) -> CaseEvidenceLink:
        case = await self._load_owned(case_id, actor)
        if await self._uow.evidence_links.get(case_id, data.evidence_id) is not None:
            raise EvidenceAlreadyLinkedError(
                f"evidence {data.evidence_id} is already linked to case {case_id}"
            )
        # NOTE: evidence_id is an inter-schema app-ref (§5) whose existence is validated
        # via ingestion.public.EvidenceService.exists — deferred until ingestion's service
        # is implemented (a different module). Flagged in the phase report.
        link = CaseEvidenceLink(
            case_id=case_id,
            evidence_id=data.evidence_id,
            linked_by_user_id=actor.user_id,
            linked_at=datetime.now(UTC),
        )
        await self._uow.evidence_links.add(link)
        await self._uow.outbox.publish(
            event_type=EVENT_EVIDENCE_LINKED_TO_CASE,
            aggregate_type="case",
            aggregate_id=case.case_id,
            payload={"case_id": str(case_id), "evidence_id": str(data.evidence_id)},
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor, EVENT_EVIDENCE_LINKED_TO_CASE, case_id, {"evidence_id": str(data.evidence_id)}
        )
        await self._uow.commit()
        return link

    async def unlink_evidence(
        self, case_id: UUID, evidence_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> None:
        await self._load_owned(case_id, actor)
        link = await self._uow.evidence_links.get(case_id, evidence_id)
        if link is None:
            raise EvidenceLinkNotFoundError(
                f"evidence {evidence_id} is not linked to case {case_id}"
            )
        await self._uow.evidence_links.remove(link)
        await self._uow.outbox.publish(
            event_type=EVENT_EVIDENCE_UNLINKED_FROM_CASE,
            aggregate_type="case",
            aggregate_id=case_id,
            payload={"case_id": str(case_id), "evidence_id": str(evidence_id)},
            correlation_id=correlation_id,
            actor_type="user",
            actor_ref=actor.user_id,
        )
        await self._audit(
            actor, EVENT_EVIDENCE_UNLINKED_FROM_CASE, case_id, {"evidence_id": str(evidence_id)}
        )
        await self._uow.commit()

    async def generate_report(
        self,
        case_id: UUID,
        data: CaseReportCreate,
        actor: CurrentUser,
        correlation_id: str,
        task_queue: TaskQueue,
    ) -> str:
        """Enqueue async report generation (api-design.md §7/§2.12). Returns the job id.

        The ``case_reports`` row is written by the job on completion (its schema requires
        a ``storage_ref``); this trigger only enqueues. The job itself is storage-blocked
        (platform/storage.py not built) — flagged in the phase report.
        """
        await self._load_owned(case_id, actor)
        job = await task_queue.enqueue_job("generate_case_report", case_id)
        await self._audit(
            actor, "case.report_requested", case_id, {"report_type": data.report_type}
        )
        await self._uow.commit()
        return getattr(job, "job_id", "") if job is not None else ""

    # -- queries ------------------------------------------------------------
    async def get_case(self, case_id: UUID, actor: CurrentUser) -> Case:
        return await self._load_owned(case_id, actor)

    async def list_cases(
        self, actor: CurrentUser, filters: CaseSearchFilters, page: PageParams
    ) -> tuple[list[Case], str | None, bool]:
        cursor_created_at: datetime | None = None
        cursor_case_id: UUID | None = None
        if page.cursor:
            raw_value, cursor_case_id = decode_cursor(page.cursor)
            cursor_created_at = datetime.fromisoformat(raw_value)
        rows = await self._uow.cases.list_cases(
            owner_id=actor.user_id,
            status=filters.status,
            created_after=filters.created_after,
            created_before=filters.created_before,
            text=filters.text,
            limit=page.limit,
            cursor_created_at=cursor_created_at,
            cursor_case_id=cursor_case_id,
        )
        has_more = len(rows) > page.limit
        items = list(rows[: page.limit])
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(last.created_at.isoformat(), last.case_id)
        return items, next_cursor, has_more

    async def search(
        self, actor: CurrentUser, filters: CaseSearchFilters, page: PageParams
    ) -> tuple[list[Case], str | None, bool]:
        """Relational search over cases — same path as ``list_cases`` (api-design 'List/search')."""
        return await self.list_cases(actor, filters, page)

    async def list_status_history(
        self, case_id: UUID, actor: CurrentUser
    ) -> Sequence[CaseStatusHistory]:
        await self._load_owned(case_id, actor)
        return await self._uow.status_history.list_for_case(case_id)

    async def timeline(self, case_id: UUID, actor: CurrentUser) -> Sequence[CaseStatusHistory]:
        """The case's temporal view — its status-history ledger (the documented timeline)."""
        return await self.list_status_history(case_id, actor)

    async def list_case_evidence(
        self, case_id: UUID, actor: CurrentUser
    ) -> Sequence[CaseEvidenceLink]:
        await self._load_owned(case_id, actor)
        return await self._uow.evidence_links.list_for_case(case_id)

    async def list_reports(self, case_id: UUID, actor: CurrentUser) -> Sequence[CaseReport]:
        await self._load_owned(case_id, actor)
        return await self._uow.reports.list_for_case(case_id)

    async def get_report(self, report_id: UUID, actor: CurrentUser) -> CaseReport:
        report = await self._uow.reports.get_by_id(report_id)
        if report is None:
            raise ReportNotFoundError()
        await self._load_owned(report.case_id, actor)  # enforce case-scoped access
        return report

    async def get_report_download_url(self, report_id: UUID, actor: CurrentUser) -> str:
        """Return the report's object-storage reference. Presigning requires the storage
        client (platform/storage.py, not yet built) — flagged in the phase report."""
        report = await self.get_report(report_id, actor)
        return report.storage_ref


def _normalize_etag(value: str) -> str:
    return value.strip().removeprefix("W/").strip('"')


class DbCaseAccessChecker:
    """Adapter implementing the platform ``CaseAccessChecker`` port (guide Part 8).

    Phase 1 access model = ownership (there is no case-membership table in
    database-design.md §3.4). Registered via ``app.dependency_overrides``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user_has_access(self, case_id: UUID, user_id: UUID) -> bool:
        from sqlalchemy import select

        result = await self._session.execute(
            select(Case.owning_user_id).where(Case.case_id == case_id)
        )
        owner = result.scalar_one_or_none()
        return owner is not None and owner == user_id


def get_case_service(
    uow: CaseManagementUnitOfWork = Depends(get_case_management_uow),
) -> CaseService:
    """FastAPI dependency constructing a ``CaseService`` on a request-scoped UoW."""
    return CaseService(uow)


def provide_case_access_checker(session: AsyncSession = Depends(get_session)) -> CaseAccessChecker:
    """Override target for platform's ``get_case_access_checker`` port."""
    return DbCaseAccessChecker(session)


__all__ = [
    "CaseSearchFilters",
    "CaseService",
    "DbCaseAccessChecker",
    "case_etag",
    "get_case_service",
    "get_task_queue",
    "provide_case_access_checker",
]
