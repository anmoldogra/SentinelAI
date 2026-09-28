"""Membership-based ABAC against a real Postgres — ADR-0017, security-architecture.md §6.

The unit tier can prove what the service *decides*; it cannot prove that the decision is one SQL
statement whose owner-or-member semantics actually hold in Postgres, which is what
``CaseMemberRepository.user_has_access`` is. A fake that returned ``case_id in self.store`` would
pass every unit test while the real query denied the owner.

So this file exercises the real table, the real query, and the real service methods. §6's worked
example is the shape it is built around: an investigator who is *not* assigned to a case must be
refused, and the refusal must leave a record.

Runs in a throwaway database created and dropped here (the ``test_auth_db.py`` pattern). Skips
cleanly when no Postgres is reachable; never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.modules.case_management.models import (
    STATUS_OPEN,
    Case,
    CaseMember,
)
from sentinelai.modules.case_management.repository import CaseMemberRepository
from sentinelai.modules.case_management.service import DbCaseAccessChecker
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_NOW = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)


async def _reachable(url: str) -> bool:
    try:
        engine = create_async_engine(url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        finally:
            await engine.dispose()
        return True
    except Exception:
        return False


async def _create_throwaway_database() -> tuple[str, str]:
    name = f"sentinelai_abactest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()
    return name, _URL.rsplit("/", 1)[0] + f"/{name}"


async def _drop_throwaway_database(name: str) -> None:
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def _create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS case_management"))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[Case.__table__, CaseMember.__table__],
        )


@pytest.fixture
async def db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _reachable(_URL):
        pytest.skip(
            f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL to run"
        )
    database, url = await _create_throwaway_database()
    engine = create_async_engine(url)
    try:
        await _create_tables(engine)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        await _drop_throwaway_database(database)


async def _seed_case(session: AsyncSession, owner_id: UUID) -> Case:
    case = Case(
        case_id=uuid4(),
        title="Operation Nightfall",
        description=None,
        status=STATUS_OPEN,
        owning_user_id=owner_id,
        created_at=_NOW,
        closed_at=None,
    )
    session.add(case)
    await session.flush()
    return case


async def test_a_stranger_is_refused_a_case_they_are_not_assigned_to(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """security-architecture.md §6's worked example, executed.

    The caller holds a legitimate role — RBAC is not what stops them. The case-scope attribute is.
    """
    async with db() as session:
        owner, stranger = uuid4(), uuid4()
        case = await _seed_case(session, owner)
        repo = CaseMemberRepository(session)

        assert await repo.user_has_access(case.case_id, owner) is True
        assert await repo.user_has_access(case.case_id, stranger) is False


async def test_a_granted_member_gains_access_and_a_revoked_one_loses_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of the table: access that is neither ownership nor nothing."""
    async with db() as session:
        owner, analyst = uuid4(), uuid4()
        case = await _seed_case(session, owner)
        repo = CaseMemberRepository(session)

        assert await repo.user_has_access(case.case_id, analyst) is False

        member = CaseMember(
            case_id=case.case_id,
            user_id=analyst,
            role="analyst",
            granted_by_user_id=owner,
            granted_at=_NOW,
        )
        await repo.add(member)
        assert await repo.user_has_access(case.case_id, analyst) is True

        await repo.remove(member)
        assert await repo.user_has_access(case.case_id, analyst) is False, (
            "revocation must take effect immediately, not at session expiry"
        )


async def test_membership_on_one_case_grants_nothing_on_another(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The grant is per case. A query missing its ``case_id`` predicate would pass every other
    test in this file and fail this one."""
    async with db() as session:
        owner, analyst = uuid4(), uuid4()
        assigned = await _seed_case(session, owner)
        other = await _seed_case(session, owner)
        repo = CaseMemberRepository(session)

        await repo.add(
            CaseMember(
                case_id=assigned.case_id,
                user_id=analyst,
                role="analyst",
                granted_by_user_id=owner,
                granted_at=_NOW,
            )
        )

        assert await repo.user_has_access(assigned.case_id, analyst) is True
        assert await repo.user_has_access(other.case_id, analyst) is False


async def test_a_membership_on_another_users_case_does_not_leak_across_users(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Symmetric to the previous test, on the other predicate: a query missing ``user_id`` would
    let any member of a case stand in for any other."""
    async with db() as session:
        owner, member_user, stranger = uuid4(), uuid4(), uuid4()
        case = await _seed_case(session, owner)
        repo = CaseMemberRepository(session)
        await repo.add(
            CaseMember(
                case_id=case.case_id,
                user_id=member_user,
                role="investigator",
                granted_by_user_id=owner,
                granted_at=_NOW,
            )
        )

        assert await repo.user_has_access(case.case_id, member_user) is True
        assert await repo.user_has_access(case.case_id, stranger) is False


async def test_access_to_a_nonexistent_case_is_denied_not_an_error(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Fail closed (security §1, §6 checklist).

    Returning ``False`` rather than raising is also what keeps the endpoint from distinguishing
    "no such case" from "not yours" — api-design.md §2.4's 403/404 ambiguity.
    """
    async with db() as session:
        repo = CaseMemberRepository(session)
        assert await repo.user_has_access(uuid4(), uuid4()) is False


async def test_the_registered_adapter_resolves_the_same_answer(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """``DbCaseAccessChecker`` is what the composition root registers behind platform's port, so
    the port's behaviour — not just the repository's — is what actually gates a request."""
    async with db() as session:
        owner, analyst = uuid4(), uuid4()
        case = await _seed_case(session, owner)
        checker = DbCaseAccessChecker(session)

        assert await checker.user_has_access(case.case_id, owner) is True
        assert await checker.user_has_access(case.case_id, analyst) is False

        session.add(
            CaseMember(
                case_id=case.case_id,
                user_id=analyst,
                role="observer",
                granted_by_user_id=owner,
                granted_at=_NOW,
            )
        )
        await session.flush()
        assert await checker.user_has_access(case.case_id, analyst) is True


async def test_a_membership_is_one_row_per_user_per_case(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The composite PK is what makes ``PUT .../members/{user_id}`` idempotent rather than
    duplicating a grant. Proven against the real constraint, not the ORM's intent."""
    from sqlalchemy.exc import IntegrityError

    async with db() as session:
        owner, analyst = uuid4(), uuid4()
        case = await _seed_case(session, owner)
        for _ in range(2):
            session.add(
                CaseMember(
                    case_id=case.case_id,
                    user_id=analyst,
                    role="analyst",
                    granted_by_user_id=owner,
                    granted_at=_NOW,
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()
