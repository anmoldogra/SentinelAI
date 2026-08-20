"""case_management business logic (guide Part 5) — real implementation.

All business rules live here: the case lifecycle state machine, optimistic-concurrency
ETag checks, evidence-link dedup, and report orchestration. Every mutation writes its
outbox event in the SAME transaction as the business write (event-driven §16) and an
audit entry (security §22). **The service never commits** (ADR-0005): the entrypoint —
router or job — owns the transaction and commits once on success. Reads enforce
ownership (defense in depth behind the HTTP ABAC guard).

Documented lifecycle (database-design.md §8 / api-design.md): ``open | closed |
archived``, initial ``open``. Transition edges (``archived`` terminal, ``open↔closed``
reversible) are inferred — the states are documented, the exact edges are not.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.case_management.events import (
    EVENT_CASE_CREATED,
    EVENT_CASE_REPORT_GENERATED,
    EVENT_CASE_STATUS_CHANGED,
    EVENT_EVIDENCE_LINKED_TO_CASE,
    EVENT_EVIDENCE_UNLINKED_FROM_CASE,
)
from sentinelai.modules.case_management.exceptions import (
    CaseNotFoundError,
    EvidenceAlreadyLinkedError,
    EvidenceLinkNotFoundError,
    ReportNotFoundError,
    ReportNotReadyError,
)
from sentinelai.modules.case_management.models import (
    REPORT_COMPLETED,
    REPORT_FAILED,
    REPORT_QUEUED,
    REPORT_RUNNING,
    STATUS_CLOSED,
    STATUS_OPEN,
    TRANSITIONS,
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
from sentinelai.platform.config import settings
from sentinelai.platform.db.session import get_session
from sentinelai.platform.storage import (
    ObjectStorage,
    build_object_uri,
    get_object_storage,
    parse_object_uri,
)
from sentinelai.platform.tasks import TaskQueue, get_task_queue
from sentinelai.shared.exceptions import (
    ForbiddenError,
    PreconditionFailedError,
)
from sentinelai.shared.pagination import PageParams, decode_cursor, encode_cursor

# The status vocabulary and machine belong to the Case aggregate (ADR-0011 §1, models.py);
# re-exported here because they are part of this service's public vocabulary too.
_TRANSITIONS = TRANSITIONS


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


# Presigned URLs are short-lived bearer credentials (ADR-0008 §6, api-design.md §7's
# "short-lived presigned URL"). Matches the evidence-download TTL.
_PRESIGN_TTL_SECONDS = 900


def _actor_role(actor: CurrentUser) -> str:
    return actor.roles[0] if actor.roles else "unknown"


class CaseService:
    """Case lifecycle, evidence linking, status history, and report orchestration."""

    def __init__(self, uow: CaseManagementUnitOfWork, *, storage: ObjectStorage) -> None:
        self._uow = uow
        self._storage = storage

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
        # The aggregate enforces the machine (ADR-0011 §1) — an illegal transition raises
        # before any orchestration side effect below can run.
        now = datetime.now(UTC)
        previous = case.transition_to(new_status, at=now)

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
                # The case's investigator — the recipient `notification` needs to act on this
                # fact without a follow-up call (§18's thin-event rule, §25.7 catalog).
                "owning_user_id": str(case.owning_user_id),
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
        return case

    async def change_status(
        self, case_id: UUID, data: CaseStatusUpdate, actor: CurrentUser, correlation_id: str
    ) -> Case:
        case = await self._load_owned(case_id, actor)
        await self._apply_transition(case, data.new_status, data.notes, actor, correlation_id)
        return case

    async def close_case(self, case_id: UUID, actor: CurrentUser, correlation_id: str) -> Case:
        """Convenience transition to ``closed`` (no dedicated endpoint — POST /status)."""
        case = await self._load_owned(case_id, actor)
        await self._apply_transition(case, STATUS_CLOSED, None, actor, correlation_id)
        return case

    async def reopen_case(self, case_id: UUID, actor: CurrentUser, correlation_id: str) -> Case:
        """Convenience transition back to ``open`` (only valid from ``closed``)."""
        case = await self._load_owned(case_id, actor)
        await self._apply_transition(case, STATUS_OPEN, None, actor, correlation_id)
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

    async def generate_report(
        self,
        case_id: UUID,
        data: CaseReportCreate,
        actor: CurrentUser,
        correlation_id: str,
        task_queue: TaskQueue,
    ) -> CaseReport:
        """Create the ``queued`` report row and enqueue its job (api-design.md §7/§2.12).

        The row IS the job's state (guide Part 12), so it is written *now* — not on completion —
        giving the client something to poll at ``GET /reports/{report_id}`` immediately. The
        enqueue happens after the insert so the job can never observe a row that does not exist
        yet; it rides the same transaction the entrypoint commits (ADR-0005).
        """
        await self._load_owned(case_id, actor)
        report = CaseReport(
            case_id=case_id,
            report_type=data.report_type,
            storage_ref=None,
            generated_by_user_id=actor.user_id,
            generated_at=None,
            status=REPORT_QUEUED,
            requested_at=datetime.now(UTC),
            failure_reason=None,
        )
        await self._uow.reports.add(report)
        await task_queue.enqueue_job("generate_case_report", case_id, report.report_id)
        await self._audit(
            actor,
            "case.report_requested",
            case_id,
            {"report_type": data.report_type, "report_id": str(report.report_id)},
        )
        return report

    async def complete_report(
        self, report_id: UUID, storage: ObjectStorage, correlation_id: str
    ) -> CaseReport:
        """Render the report, store it, and mark the row completed. Invoked by the worker job.

        Phase 1 renders a JSON dump of the case, its evidence links, and its status history —
        deliberately not a PDF. The object is streamed through the ``ObjectStorage`` port, so this
        module never imports an S3 client, and the row is only marked ``completed`` after the
        upload returns: a crash mid-upload leaves it ``running`` for arq to retry rather than
        advertising a report that is not there.

        Idempotent: an already-completed report is returned untouched, so a job redelivery neither
        re-uploads nor re-publishes. Never commits (ADR-0005) — the job wrapper owns the
        transaction.
        """
        report = await self._uow.reports.get_by_id(report_id)
        if report is None:
            raise ReportNotFoundError()
        if report.status == REPORT_COMPLETED:
            return report  # a retry of a finished job

        case = await self._uow.cases.get_by_id(report.case_id)
        if case is None:
            raise CaseNotFoundError()

        report.status = REPORT_RUNNING
        document = await self._render_report(case, report)
        payload = json.dumps(document, indent=2, sort_keys=True).encode()
        bucket = settings.storage_bucket
        key = f"reports/{report.case_id}/{report.report_id}.json"

        async def _stream() -> AsyncIterator[bytes]:
            yield payload

        await storage.put_stream(bucket, key, _stream(), content_type="application/json")

        report.storage_ref = build_object_uri(bucket, key)
        report.generated_at = datetime.now(UTC)
        report.status = REPORT_COMPLETED
        report.failure_reason = None

        await self._uow.outbox.publish(
            event_type=EVENT_CASE_REPORT_GENERATED,
            aggregate_type="case_report",
            aggregate_id=report.report_id,
            payload={
                "case_id": str(report.case_id),
                "report_id": str(report.report_id),
                # The requester — the recipient `notification` alerts (§25.7).
                "requested_by_user_id": str(report.generated_by_user_id),
            },
            correlation_id=correlation_id,
            actor_type="system",
        )
        return report

    async def _render_report(self, case: Case, report: CaseReport) -> dict[str, Any]:
        """Assemble the Phase-1 JSON document: the case, its evidence links, its status history."""
        links = await self._uow.evidence_links.list_for_case(case.case_id)
        history = await self._uow.status_history.list_for_case(case.case_id)
        return {
            "report": {
                "report_id": str(report.report_id),
                "report_type": report.report_type,
                "generated_at": datetime.now(UTC).isoformat(),
            },
            "case": {
                "case_id": str(case.case_id),
                "title": case.title,
                "description": case.description,
                "status": case.status,
                "owning_user_id": str(case.owning_user_id),
                "created_at": case.created_at.isoformat(),
                "closed_at": case.closed_at.isoformat() if case.closed_at else None,
            },
            "evidence": [
                {
                    "evidence_id": str(link.evidence_id),
                    "linked_by_user_id": str(link.linked_by_user_id),
                    "linked_at": link.linked_at.isoformat(),
                }
                for link in links
            ],
            "status_history": [
                {
                    "previous_status": entry.previous_status,
                    "new_status": entry.new_status,
                    "changed_at": entry.changed_at.isoformat(),
                    "notes": entry.notes,
                }
                for entry in history
            ],
        }

    async def fail_report(self, report_id: UUID, reason: str) -> None:
        """Record that generation failed, so a client polling the row learns why (§7)."""
        report = await self._uow.reports.get_by_id(report_id)
        if report is not None and report.status != REPORT_COMPLETED:
            report.status = REPORT_FAILED
            report.failure_reason = reason

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
        """Return a short-lived presigned GET URL for a completed report (api-design.md §7).

        A report leaving the system is disclosure-significant, so generating the URL writes a
        ``platform.audit_log`` entry — the audit records the *intent to disclose* at the moment
        the credential is minted, which is the only moment the platform can observe; the
        subsequent fetch goes straight to object storage and never touches this process.

        Presigning happens before the audit write: if the audit write fails the transaction rolls
        back and the URL is never returned to the caller, so no un-audited credential escapes.
        The URL itself is never logged or audited — it is a bearer credential (ADR-0008 §6).

        A report that has not finished has no object to point at and is refused (409).
        """
        report = await self.get_report(report_id, actor)
        if report.status != REPORT_COMPLETED or not report.storage_ref:
            raise ReportNotReadyError(f"report is '{report.status}', not completed")

        bucket, key = parse_object_uri(report.storage_ref)
        url = await self._storage.presigned_download_url(
            bucket, key, expires_in=_PRESIGN_TTL_SECONDS
        )
        await self._audit(
            actor,
            "case.report_downloaded",
            report_id,
            {"report_type": report.report_type, "case_id": str(report.case_id)},
        )
        return url


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
    storage: ObjectStorage = Depends(get_object_storage),
) -> CaseService:
    """FastAPI dependency constructing a ``CaseService`` on a request-scoped UoW."""
    return CaseService(uow, storage=storage)


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
