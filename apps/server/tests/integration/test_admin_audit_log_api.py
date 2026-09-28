"""The audit-log export — api-design.md §10, PRD FR-9.3.

Built on a **real** chain: entries are written through ``record_audit_event`` against Postgres, so
the hashes, signatures and links are the production ones. That matters more here than anywhere else
in the suite — the endpoint's whole purpose is to let an outside reviewer verify the chain, and a
fixture that fabricated `entry_hash` values would test the export's plumbing while proving nothing
about what it exports.

Skips cleanly when no Postgres is reachable; never fakes a pass.
"""

from __future__ import annotations

import base64
import itertools
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.entrypoints.http.exception_handlers import register_exception_handlers
from sentinelai.entrypoints.http.middleware import register_middleware
from sentinelai.platform.admin import admin_router
from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.db.base import Base
from sentinelai.platform.db.session import get_session
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
    name = f"sentinelai_auditapi_{uuid.uuid4().hex[:8]}"
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


def _app(db: async_sessionmaker[AsyncSession], actor: CurrentUser) -> FastAPI:
    application = FastAPI()
    register_middleware(application)
    register_exception_handlers(application)
    application.include_router(admin_router)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with db() as session:
            yield session

    application.dependency_overrides[get_current_user] = lambda: actor
    application.dependency_overrides[get_kms] = lambda: kms_for_tests()
    application.dependency_overrides[get_session] = _session
    return application


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _write_entries(db: async_sessionmaker[AsyncSession], specs: list[dict[str, Any]]) -> None:
    """Append real, signed, chained audit entries — one transaction, in order."""
    async with db() as session:
        for spec in specs:
            await record_audit_event(
                session,
                kms=kms_for_tests(),
                actor_user_id=spec.get("actor_user_id"),
                actor_role=spec.get("actor_role", "investigator"),
                action=spec["action"],
                module=spec.get("module", "case_management"),
                target_type=spec.get("target_type", "case"),
                target_id=spec.get("target_id") or uuid4(),
                ip_address=spec.get("ip_address"),
                user_agent=spec.get("user_agent"),
                details=spec.get("details"),
            )
        await session.commit()


_COMPLIANCE = CurrentUser(user_id=uuid4(), roles=("compliance",))
_ADMIN = CurrentUser(user_id=uuid4(), roles=("admin",))
_ANALYST = CurrentUser(user_id=uuid4(), roles=("investigator",))


# --- authorization ----------------------------------------------------------
@pytest.mark.parametrize("actor", [_ADMIN, _COMPLIANCE])
async def test_admin_and_compliance_may_export(
    db: async_sessionmaker[AsyncSession], actor: CurrentUser
) -> None:
    """api-design.md §4.1 grants this endpoint to both. `compliance` is the point: an audit export
    is what an oversight body reads, and requiring `admin` would mean the people reviewing the
    operators had to be operators."""
    await _write_entries(db, [{"action": "case_created"}])
    async with await _client(_app(db, actor)) as client:
        response = await client.get("/api/v1/admin/audit-log")
    assert response.status_code == 200


async def test_an_investigator_is_refused(db: async_sessionmaker[AsyncSession]) -> None:
    """The audit log records what every analyst did; letting one read it wholesale would hand an
    insider the map of who is watching them."""
    await _write_entries(db, [{"action": "case_created"}])
    async with await _client(_app(db, _ANALYST)) as client:
        response = await client.get("/api/v1/admin/audit-log")
    assert response.status_code == 403


# --- the hash chain ---------------------------------------------------------
async def test_the_export_carries_the_chain_links(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """§10: the response includes each entry's `prev_entry_hash`/`entry_hash` "so an external
    reviewer can independently verify the hash chain hasn't been tampered with"."""
    await _write_entries(db, [{"action": "case_created"}, {"action": "case_status_changed"}])
    async with await _client(_app(db, _COMPLIANCE)) as client:
        response = await client.get("/api/v1/admin/audit-log")

    entries = response.json()["data"]
    assert len(entries) == 2
    for entry in entries:
        assert entry["prev_entry_hash"]
        assert entry["entry_hash"]


async def test_the_exported_chain_is_continuous_and_in_order(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The property a reviewer actually checks: each entry's `prev_entry_hash` is its predecessor's
    `entry_hash`.

    This is why the endpoint sorts **ascending** by `(occurred_at, audit_id)`. Newest-first would be
    friendlier to a UI and would hand a reviewer a sequence whose links run backwards; ordering by
    `occurred_at` alone would be worse, because two entries written in the same transaction can
    share a timestamp and the pair could come back either way round.
    """
    await _write_entries(
        db,
        [
            {"action": "evidence_ingested"},
            {"action": "evidence_linked"},
            {"action": "case_closed"},
        ],
    )
    async with await _client(_app(db, _COMPLIANCE)) as client:
        response = await client.get("/api/v1/admin/audit-log")

    entries = response.json()["data"]
    assert [e["action"] for e in entries] == [
        "evidence_ingested",
        "evidence_linked",
        "case_closed",
    ]
    for previous, current in itertools.pairwise(entries):
        assert current["prev_entry_hash"] == previous["entry_hash"], (
            "a gap here is either tampering or a broken export — a reviewer cannot tell which, "
            "which is exactly why the endpoint must not introduce one"
        )


async def test_the_signature_is_exported_as_base64(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Recomputing hashes only proves the entries are self-consistent — an insider who rewrote the
    whole chain would pass that. Only the signature proves they could not (ADR-0003 §1), so the
    export carries it along with the algorithm and key id needed to verify it.
    """
    await _write_entries(db, [{"action": "case_created"}])
    async with await _client(_app(db, _COMPLIANCE)) as client:
        response = await client.get("/api/v1/admin/audit-log")

    entry = response.json()["data"][0]
    assert entry["sig_alg"] and entry["key_id"]
    assert entry["hash_algo"] and entry["preimage_version"] is not None
    assert entry["signature"], "an unsigned export cannot be independently verified"
    # Decodable, and the exact encoding a reviewer's script will assume.
    assert base64.b64decode(entry["signature"])


# --- filtering --------------------------------------------------------------
async def test_filtering_by_actor(db: async_sessionmaker[AsyncSession]) -> None:
    wanted, other = uuid4(), uuid4()
    await _write_entries(
        db,
        [
            {"action": "a", "actor_user_id": wanted},
            {"action": "b", "actor_user_id": other},
            {"action": "c", "actor_user_id": wanted},
        ],
    )
    async with await _client(_app(db, _COMPLIANCE)) as client:
        response = await client.get(
            "/api/v1/admin/audit-log", params={"actor_user_id": str(wanted)}
        )

    entries = response.json()["data"]
    assert [e["action"] for e in entries] == ["a", "c"]
    assert {e["actor_user_id"] for e in entries} == {str(wanted)}


async def test_filtering_by_action(db: async_sessionmaker[AsyncSession]) -> None:
    await _write_entries(
        db, [{"action": "login_failed"}, {"action": "login_success"}, {"action": "login_failed"}]
    )
    async with await _client(_app(db, _COMPLIANCE)) as client:
        response = await client.get("/api/v1/admin/audit-log", params={"action": "login_failed"})

    entries = response.json()["data"]
    assert len(entries) == 2
    assert {e["action"] for e in entries} == {"login_failed"}


async def test_filtering_by_target(db: async_sessionmaker[AsyncSession]) -> None:
    case_id = uuid4()
    await _write_entries(
        db,
        [
            {"action": "a", "target_type": "case", "target_id": case_id},
            {"action": "b", "target_type": "evidence"},
        ],
    )
    async with await _client(_app(db, _COMPLIANCE)) as client:
        by_type = await client.get("/api/v1/admin/audit-log", params={"target_type": "evidence"})
        by_id = await client.get("/api/v1/admin/audit-log", params={"target_id": str(case_id)})

    assert [e["action"] for e in by_type.json()["data"]] == ["b"]
    assert [e["action"] for e in by_id.json()["data"]] == ["a"]


async def test_filtering_by_time_window(db: async_sessionmaker[AsyncSession]) -> None:
    """The window an oversight request actually arrives as ("everything between these dates")."""
    await _write_entries(db, [{"action": "inside"}])
    async with await _client(_app(db, _COMPLIANCE)) as client:
        before = datetime.now(UTC) - timedelta(hours=1)
        after = datetime.now(UTC) + timedelta(hours=1)
        in_window = await client.get(
            "/api/v1/admin/audit-log",
            params={"occurred_after": before.isoformat(), "occurred_before": after.isoformat()},
        )
        past_window = await client.get(
            "/api/v1/admin/audit-log", params={"occurred_after": after.isoformat()}
        )

    assert [e["action"] for e in in_window.json()["data"]] == ["inside"]
    assert past_window.json()["data"] == []


async def test_filters_combine(db: async_sessionmaker[AsyncSession]) -> None:
    """Each filter is an independent WHERE clause, so two of them must intersect rather than one
    quietly winning."""
    actor = uuid4()
    await _write_entries(
        db,
        [
            {"action": "login_failed", "actor_user_id": actor},
            {"action": "login_success", "actor_user_id": actor},
            {"action": "login_failed", "actor_user_id": uuid4()},
        ],
    )
    async with await _client(_app(db, _COMPLIANCE)) as client:
        response = await client.get(
            "/api/v1/admin/audit-log",
            params={"actor_user_id": str(actor), "action": "login_failed"},
        )

    entries = response.json()["data"]
    assert len(entries) == 1
    assert entries[0]["actor_user_id"] == str(actor)


async def test_a_filter_value_is_not_interpreted_as_sql(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """security-architecture.md §27: parameterized queries, unconditionally. The values here are
    reviewer-supplied query strings."""
    await _write_entries(db, [{"action": "case_created"}])
    async with await _client(_app(db, _COMPLIANCE)) as client:
        response = await client.get("/api/v1/admin/audit-log", params={"action": "' OR 1=1 --"})

    assert response.status_code == 200
    assert response.json()["data"] == [], "treated as a literal that matches nothing"


# --- pagination -------------------------------------------------------------
async def test_pagination_walks_the_whole_chain_without_gaps(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Keyset pagination over a composite key, checked the way it can actually break: page the
    whole log two at a time and assert the reassembled sequence is every entry, once, in order —
    and that the links still join across the page boundaries.
    """
    actions = [f"event_{i:02d}" for i in range(7)]
    await _write_entries(db, [{"action": a} for a in actions])

    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    async with await _client(_app(db, _COMPLIANCE)) as client:
        for _ in range(10):  # bounded, so a broken cursor fails rather than loops forever
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            page = (await client.get("/api/v1/admin/audit-log", params=params)).json()
            collected.extend(page["data"])
            cursor = page["pagination"]["next_cursor"]
            if not page["pagination"]["has_more"]:
                break

    assert [e["action"] for e in collected] == actions, "every entry, once, in chain order"
    for previous, current in itertools.pairwise(collected):
        assert current["prev_entry_hash"] == previous["entry_hash"], (
            "the chain must still join across a page boundary"
        )


async def test_the_last_page_reports_no_more(db: async_sessionmaker[AsyncSession]) -> None:
    await _write_entries(db, [{"action": "only"}])
    async with await _client(_app(db, _COMPLIANCE)) as client:
        page = (await client.get("/api/v1/admin/audit-log", params={"limit": 50})).json()

    assert page["pagination"]["has_more"] is False
    assert page["pagination"]["next_cursor"] is None


async def test_an_empty_log_is_an_empty_page_not_an_error(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with await _client(_app(db, _COMPLIANCE)) as client:
        response = await client.get("/api/v1/admin/audit-log")

    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["pagination"]["has_more"] is False


# --- no erasure path --------------------------------------------------------
@pytest.mark.parametrize("method", ["delete", "post", "patch", "put"])
async def test_there_is_no_write_path_on_this_endpoint(
    db: async_sessionmaker[AsyncSession], method: str
) -> None:
    """api-design.md §10: "There is no `DELETE` anywhere in this endpoint group — the audit log has
    no API-level erasure path at all."

    Asserted rather than assumed, because the day someone adds a convenience mutation here is the
    day the audit log stops being evidence.
    """
    async with await _client(_app(db, _ADMIN)) as client:
        response = await getattr(client, method)("/api/v1/admin/audit-log")

    assert response.status_code == 405
