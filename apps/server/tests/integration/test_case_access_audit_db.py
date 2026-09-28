"""An ABAC denial leaves a record — security-architecture.md §6, ADR-0017 §4.

§6 requires the denial specifically: "the denial itself is written to ``platform.audit_log`` with
the caller's identity, the resource requested, and the reason", so a compliance review can
distinguish "this analyst never had access" from "this analyst had access and used it".

Two properties need a real database to demonstrate, and neither can be faked:

* the entry is a **signed, hash-chained** audit row like any other, not a log line;
* it **survives its own request**. The denial path ends in ``ForbiddenError``, and ADR-0005's
  boundary rolls back on any exception — so the write has to commit itself, or the record §6
  requires would be erased by the very response that makes it interesting.

Skips cleanly when no Postgres is reachable; never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

from sentinelai.platform.auth.audit import record_audit_event as _real_record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser, _audit_case_access_denied
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)


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
    name = f"sentinelai_denytest_{uuid.uuid4().hex[:8]}"
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
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
        await conn.run_sync(Base.metadata.create_all, tables=[AuditLog.__table__])


@pytest.fixture(autouse=True)
def _real_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the root conftest's DB-less audit stub — this file is the one that must not be stubbed.

    ``tests/conftest.py`` replaces ``record_audit_event`` in the dependencies module with a no-op
    so DB-less API tests are not dragged into Postgres just to be refused. That is right for them
    and fatal here: the whole claim under test is that the real function writes a real row.
    Module-level autouse fixtures run after the root conftest's, so this wins.
    """
    monkeypatch.setattr(
        "sentinelai.platform.auth.dependencies.record_audit_event", _real_record_audit_event
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


def _request() -> Request:
    """A real ASGI request, so ``request.client`` and the headers behave as they do in production
    rather than as a stand-in decides they should."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/cases/x",
            "raw_path": b"/api/v1/cases/x",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"user-agent", b"curl/8.7")],
            "client": ("10.4.2.9", 54321),
            "server": ("testserver", 80),
        }
    )


async def test_a_denial_is_recorded_with_who_what_and_why(
    db: async_sessionmaker[AsyncSession],
) -> None:
    actor = CurrentUser(user_id=uuid4(), roles=("investigator", "compliance"))
    case_id = uuid4()

    async with db() as session:
        await _audit_case_access_denied(
            session,
            kms=kms_for_tests(),
            request=_request(),
            current_user=actor,
            case_id=case_id,
        )

    async with db() as session:
        entries = (await session.execute(select(AuditLog))).scalars().all()

    assert len(entries) == 1
    entry = entries[0]
    assert entry.action == "case_access_denied"
    assert entry.actor_user_id == actor.user_id, "§6: the caller's identity"
    assert entry.target_type == "case" and entry.target_id == case_id, "§6: the resource"
    assert entry.details is not None
    assert entry.details["reason"] == "no_case_scope", "§6: the reason"
    assert entry.details["roles"] == ["investigator", "compliance"], (
        "the full role set, so a reviewer can tell 'wrong role' from 'right role, wrong case'"
    )
    assert entry.ip_address == "10.4.2.9"
    assert entry.user_agent == "curl/8.7"


async def test_the_denial_entry_commits_itself(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The property ADR-0005's boundary would otherwise destroy.

    The session here is never committed by the test, and is then rolled back explicitly — exactly
    what ``TransactionalRoute`` does when the ``ForbiddenError`` that follows this call reaches it.
    The row must still be there.
    """
    actor = CurrentUser(user_id=uuid4(), roles=("investigator",))

    async with db() as session:
        await _audit_case_access_denied(
            session,
            kms=kms_for_tests(),
            request=_request(),
            current_user=actor,
            case_id=uuid4(),
        )
        await session.rollback()

    async with db() as session:
        entries = (await session.execute(select(AuditLog))).scalars().all()

    assert len(entries) == 1, (
        "the denial audit must survive the boundary rollback that its own 403 triggers"
    )


async def test_the_denial_entry_is_signed_and_chained(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A denial is a ledger entry like any other (ADR-0003 §1): it carries a signature and links
    to its predecessor, so an insider cannot quietly delete the record of being refused."""
    actor = CurrentUser(user_id=uuid4(), roles=("investigator",))

    async with db() as session:
        for _ in range(2):
            await _audit_case_access_denied(
                session,
                kms=kms_for_tests(),
                request=_request(),
                current_user=actor,
                case_id=uuid4(),
            )

    async with db() as session:
        entries = (
            (await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
        )

    assert len(entries) == 2
    for entry in entries:
        assert entry.signature, "every ledger entry is signed under EVIDENCE_ROOT"
        assert entry.key_id and entry.sig_alg
        assert entry.entry_hash and entry.prev_entry_hash
    assert entries[1].prev_entry_hash == entries[0].entry_hash, "the chain must link"


async def test_an_anonymous_denial_still_records_the_attempt(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """A principal with no roles is unusual but representable, and the entry must not be lost to
    an ``IndexError`` reaching for ``roles[0]``."""
    actor = CurrentUser(user_id=uuid4(), roles=())

    async with db() as session:
        await _audit_case_access_denied(
            session,
            kms=kms_for_tests(),
            request=_request(),
            current_user=actor,
            case_id=uuid4(),
        )

    async with db() as session:
        entry = (await session.execute(select(AuditLog))).scalars().one()

    assert entry.actor_role == "none"
    assert entry.details is not None and entry.details["roles"] == []
    assert entry.actor_user_id is not None, "the principal was authenticated, just unprivileged"
