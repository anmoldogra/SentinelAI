"""case_management persistence + the module's Unit of Work (guide Part 3).

Repositories persist only — no business rules (those live in ``service.py``). Each
repository knows only this module's models; it structurally cannot query another
module's tables. The concrete ``CaseManagementUnitOfWork`` subclasses the generic
platform base and attaches this module's repositories + an ``OutboxWriter`` bound to
the ``case_management`` schema — so a business write and its outbox event share one
transaction (event-driven §16).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from fastapi import Depends
from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from sentinelai.modules.case_management.models import (
    Case,
    CaseEvidenceLink,
    CaseMember,
    CaseReport,
    CaseStatusHistory,
)
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.crypto.kms import KeyManagementService
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.uow import UnitOfWork
from sentinelai.platform.events.outbox import OutboxWriter
from sentinelai.platform.events.signing import EventSigner

_SCHEMA = "case_management"


class CaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, case_id: UUID) -> Case | None:
        result = await self._session.execute(select(Case).where(Case.case_id == case_id))
        return result.scalar_one_or_none()

    async def add(self, case: Case) -> None:
        self._session.add(case)
        await self._session.flush()  # surface integrity errors inside the transaction

    async def list_cases(
        self,
        *,
        owner_id: UUID,
        status: str | None,
        created_after: datetime | None,
        created_before: datetime | None,
        text: str | None,
        limit: int,
        cursor_created_at: datetime | None,
        cursor_case_id: UUID | None,
    ) -> Sequence[Case]:
        """Keyset-paginated list, newest first, scoped to ``owner_id``.

        Returns up to ``limit + 1`` rows so the service can compute ``has_more``.
        """
        stmt = select(Case).where(Case.owning_user_id == owner_id)
        if status is not None:
            stmt = stmt.where(Case.status == status)
        if created_after is not None:
            stmt = stmt.where(Case.created_at >= created_after)
        if created_before is not None:
            stmt = stmt.where(Case.created_at <= created_before)
        if text:
            like = f"%{text}%"
            stmt = stmt.where(or_(Case.title.ilike(like), Case.description.ilike(like)))
        if cursor_created_at is not None and cursor_case_id is not None:
            stmt = stmt.where(
                tuple_(Case.created_at, Case.case_id) < (cursor_created_at, cursor_case_id)
            )
        stmt = stmt.order_by(Case.created_at.desc(), Case.case_id.desc()).limit(limit + 1)
        result = await self._session.execute(stmt)
        return result.scalars().all()


class CaseEvidenceLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, link: CaseEvidenceLink) -> None:
        self._session.add(link)
        await self._session.flush()

    async def get(self, case_id: UUID, evidence_id: UUID) -> CaseEvidenceLink | None:
        result = await self._session.execute(
            select(CaseEvidenceLink).where(
                CaseEvidenceLink.case_id == case_id,
                CaseEvidenceLink.evidence_id == evidence_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_case(self, case_id: UUID) -> Sequence[CaseEvidenceLink]:
        result = await self._session.execute(
            select(CaseEvidenceLink)
            .where(CaseEvidenceLink.case_id == case_id)
            .order_by(CaseEvidenceLink.linked_at.desc())
        )
        return result.scalars().all()

    async def remove(self, link: CaseEvidenceLink) -> None:
        await self._session.delete(link)


class CaseStatusHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: CaseStatusHistory) -> None:
        self._session.add(entry)
        await self._session.flush()

    async def list_for_case(self, case_id: UUID) -> Sequence[CaseStatusHistory]:
        result = await self._session.execute(
            select(CaseStatusHistory)
            .where(CaseStatusHistory.case_id == case_id)
            .order_by(CaseStatusHistory.changed_at.desc())
        )
        return result.scalars().all()


class CaseReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, report: CaseReport) -> None:
        self._session.add(report)
        await self._session.flush()

    async def get_by_id(self, report_id: UUID) -> CaseReport | None:
        result = await self._session.execute(
            select(CaseReport).where(CaseReport.report_id == report_id)
        )
        return result.scalar_one_or_none()

    async def list_for_case(self, case_id: UUID) -> Sequence[CaseReport]:
        result = await self._session.execute(
            select(CaseReport)
            .where(CaseReport.case_id == case_id)
            .order_by(CaseReport.generated_at.desc())
        )
        return result.scalars().all()


class CaseMemberRepository:
    """Persists case-scope access grants (ADR-0017 §1)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, case_id: UUID, user_id: UUID) -> CaseMember | None:
        result = await self._session.execute(
            select(CaseMember).where(CaseMember.case_id == case_id, CaseMember.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def list_for_case(self, case_id: UUID) -> Sequence[CaseMember]:
        result = await self._session.execute(
            select(CaseMember)
            .where(CaseMember.case_id == case_id)
            .order_by(CaseMember.granted_at.asc())
        )
        return result.scalars().all()

    async def add(self, member: CaseMember) -> None:
        self._session.add(member)
        await self._session.flush()

    async def remove(self, member: CaseMember) -> None:
        await self._session.delete(member)
        await self._session.flush()

    async def user_has_access(self, case_id: UUID, user_id: UUID) -> bool:
        """Owner OR member, in one round trip — the ABAC case-scope decision (ADR-0017 §2).

        A single ``EXISTS`` over the union rather than "fetch the owner, then maybe fetch the
        membership": the two-query form makes the *owner's* check cheap and everyone else's cost
        an extra round trip, which is backwards once a case has a team.

        Returns a plain ``bool`` and never raises for a missing case. A caller asking about a
        case that does not exist has no access to it, which is the answer that keeps the endpoint
        from distinguishing "no such case" from "not yours" (api-design.md §2.4's 403/404
        ambiguity, security-architecture.md §6).
        """
        owner_match = select(Case.case_id).where(
            Case.case_id == case_id, Case.owning_user_id == user_id
        )
        member_match = select(CaseMember.case_id).where(
            CaseMember.case_id == case_id, CaseMember.user_id == user_id
        )
        result = await self._session.execute(select(owner_match.exists() | member_match.exists()))
        return bool(result.scalar_one())


class CaseManagementUnitOfWork(UnitOfWork):
    """Transaction boundary exposing this module's repositories + outbox."""

    def __init__(self, session: AsyncSession, *, kms: KeyManagementService | None = None) -> None:
        super().__init__(session)
        self.cases = CaseRepository(session)
        self.evidence_links = CaseEvidenceLinkRepository(session)
        self.status_history = CaseStatusHistoryRepository(session)
        self.reports = CaseReportRepository(session)
        self.members = CaseMemberRepository(session)
        self.outbox = OutboxWriter(
            session,
            schema=_SCHEMA,
            # ADR-0007 §1: events this module publishes are signed under EVENT_ROOT. `None`
            # writes them unsigned, which the dispatcher's strict mode then refuses -- so an
            # unwired publisher fails loudly at consume time rather than silently here.
            signer=EventSigner(kms) if kms is not None else None,
        )


async def get_case_management_uow(
    session: AsyncSession = Depends(get_session),
    kms: KeyManagementService = Depends(get_kms),
) -> CaseManagementUnitOfWork:
    """FastAPI dependency yielding a request-scoped case_management UoW."""
    return CaseManagementUnitOfWork(session, kms=kms)
