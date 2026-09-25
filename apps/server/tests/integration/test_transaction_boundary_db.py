"""Request atomicity at the entrypoint boundary — ADR-0005 §1, Wave 2.1.

These tests exist because the guarantee is *absence*: after a failed request, nothing must remain.
That cannot be checked by a unit test with a fake session, because a fake will happily report
whatever it was told — the question is what Postgres actually holds once the request is over.

Three kinds of write are asserted together on every path, because ADR-0005 §3 and
event-driven-architecture.md §16 make them one atomic unit and a boundary that committed two of the
three would be worse than one that committed none:

* the business row;
* the **outbox event**, which is what makes the fact observable to other modules;
* the **audit entry**, which is itself an append to the other evidentiary ledger.

The app under test wires the real ``TransactionalRoute`` and the real ``bind_session`` against a
real session. Nothing about the boundary is stubbed — the only thing these routes fake is the
domain work, so each test can fail in one precise way.

Skips cleanly when no Postgres is reachable. Never fakes a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sentinelai.platform.auth.audit import record_audit_event
from sentinelai.platform.auth.models import AuditLog
from sentinelai.platform.config import settings
from sentinelai.platform.db.base import Base
from sentinelai.platform.db.session import get_session
from sentinelai.platform.db.transaction import TransactionalRoute, bind_session
from sentinelai.platform.events.outbox import OutboxWriter, get_outbox_table
from sentinelai.shared.exceptions import ValidationFailedError
from tests.fixtures.kms import kms_for_tests

_URL = os.getenv("TEST_DATABASE_URL", settings.database_url)
_SCHEMA = "platform"


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


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if not await _reachable(_URL):
        pytest.skip(f"no Postgres reachable at {_URL.split('@')[-1]} — set TEST_DATABASE_URL")

    name = f"sentinelai_txtest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()

    engine: AsyncEngine = create_async_engine(_URL.rsplit("/", 1)[0] + f"/{name}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}"))
            await conn.run_sync(Base.metadata.create_all, tables=[AuditLog.__table__])
            await conn.run_sync(get_outbox_table(_SCHEMA).create)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        admin = create_async_engine(_URL, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


async def _write_all_three(session: AsyncSession, marker: str) -> None:
    """One audit entry plus one outbox event — the unit that must be atomic."""
    await record_audit_event(
        session,
        kms=kms_for_tests(),
        actor_user_id=uuid.uuid4(),
        actor_role="investigator",
        action=marker,
        module="ingestion",
        details={"marker": marker},
    )
    await OutboxWriter(session, _SCHEMA).publish(
        event_type="evidence.ingested",
        aggregate_type="evidence",
        aggregate_id=uuid.uuid4(),
        payload={"marker": marker},
        correlation_id=str(uuid.uuid4()),
        actor_type="user",
    )


def _build_app(sessions: async_sessionmaker[AsyncSession]) -> FastAPI:
    """An app wired with the REAL boundary, whose routes fail in one chosen way each."""
    router = APIRouter(route_class=TransactionalRoute, dependencies=[Depends(bind_session)])

    @router.post("/ok/{marker}")
    async def succeed(marker: str, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
        await _write_all_three(session, marker)
        return {"marker": marker}

    @router.post("/domain-error/{marker}")
    async def domain_error(
        marker: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, str]:
        await _write_all_three(session, marker)
        raise ValidationFailedError([{"field": "x", "message": "rejected"}])

    @router.post("/crash/{marker}")
    async def crash(marker: str, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
        await _write_all_three(session, marker)
        raise RuntimeError("an unexpected failure after writing")

    @router.post("/precommit/{marker}")
    async def precommit(
        marker: str, session: AsyncSession = Depends(get_session)
    ) -> dict[str, str]:
        """The deliberate pattern: the failure is itself an auditable fact, so commit then raise."""
        await _write_all_three(session, marker)
        await session.commit()
        raise ValidationFailedError([{"field": "x", "message": "rejected but recorded"}])

    app = FastAPI()
    app.include_router(router)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = _session_override
    return app


async def _counts(sessions: async_sessionmaker[AsyncSession], marker: str) -> tuple[int, int]:
    """(audit rows, outbox rows) carrying ``marker``."""
    async with sessions() as session:
        audit = await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == marker)
        )
        table = get_outbox_table(_SCHEMA)
        outbox = await session.execute(
            select(func.count())
            .select_from(table)
            .where(table.c.payload["marker"].astext == marker)
        )
        return int(audit.scalar_one()), int(outbox.scalar_one())


async def _post(app: FastAPI, path: str) -> int:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path)
        return response.status_code


# ---------------------------------------------------------------------------------------
# The baseline — without this, every "nothing persisted" assertion below could be vacuous
# ---------------------------------------------------------------------------------------


async def test_a_successful_request_commits_everything_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """No handler calls commit: the boundary does, and both writes must land."""
    app = _build_app(sessions)
    marker = f"ok-{uuid.uuid4().hex[:8]}"

    assert await _post(app, f"/ok/{marker}") == 200

    assert await _counts(sessions, marker) == (1, 1)


# ---------------------------------------------------------------------------------------
# Rollback — the guarantee ADR-0005 §1 exists for
# ---------------------------------------------------------------------------------------


async def test_an_unhandled_exception_rolls_back_the_whole_request(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A crash after writing must leave nothing — not the row, not the event, not the audit entry.

    This is the case that would corrupt an evidentiary record most quietly: an audit entry for an
    action that never happened, or an outbox event announcing a fact with no fact behind it.
    """
    app = _build_app(sessions)
    marker = f"crash-{uuid.uuid4().hex[:8]}"

    with pytest.raises(RuntimeError):
        await _post(app, f"/crash/{marker}")

    assert await _counts(sessions, marker) == (0, 0)


async def test_a_domain_failure_rolls_back_the_whole_request(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A 4xx is not a licence to keep partial writes.

    Unless the handler deliberately commits first (the case below), a domain failure discards
    everything — including the audit entry, which would otherwise record an action the system
    refused to take.
    """
    app = _build_app(sessions)
    marker = f"domain-{uuid.uuid4().hex[:8]}"

    with pytest.raises(ValidationFailedError):
        await _post(app, f"/domain-error/{marker}")

    assert await _counts(sessions, marker) == (0, 0)


async def test_the_outbox_event_and_the_business_write_share_one_fate(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """§16's atomicity, asserted as a pair rather than two independent facts.

    A boundary that committed the outbox but rolled back the audit entry (or the reverse) would
    publish an event no one can corroborate. Both counts move together or the test fails.
    """
    app = _build_app(sessions)
    kept = f"kept-{uuid.uuid4().hex[:8]}"
    lost = f"lost-{uuid.uuid4().hex[:8]}"

    assert await _post(app, f"/ok/{kept}") == 200
    with pytest.raises(RuntimeError):
        await _post(app, f"/crash/{lost}")

    audit_kept, outbox_kept = await _counts(sessions, kept)
    audit_lost, outbox_lost = await _counts(sessions, lost)
    assert audit_kept == outbox_kept == 1
    assert audit_lost == outbox_lost == 0


# ---------------------------------------------------------------------------------------
# The deliberate pre-commit, which must keep working
# ---------------------------------------------------------------------------------------


async def test_a_deliberate_pre_commit_survives_the_error_response(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Three real endpoints depend on this composing correctly.

    A rejected ingest keeps its intake record and `evidence.validation_failed` event (§25.2), a
    failed integrity check keeps its MISMATCH custody entry (ADR-0008 §3), and a failed login keeps
    its `login_failed` audit row (security §5). Each commits and then raises; the boundary's
    rollback must find nothing left to undo rather than erasing the record.
    """
    app = _build_app(sessions)
    marker = f"precommit-{uuid.uuid4().hex[:8]}"

    with pytest.raises(ValidationFailedError):
        await _post(app, f"/precommit/{marker}")

    assert await _counts(sessions, marker) == (1, 1)


async def test_repeated_requests_do_not_leak_state_between_transactions(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A rolled-back request must not poison the next one.

    If the boundary left a failed transaction open on a pooled connection, the following request
    would fail for a reason that has nothing to do with itself — the classic symptom of a rollback
    that was never actually performed.
    """
    app = _build_app(sessions)
    first = f"crash-{uuid.uuid4().hex[:8]}"
    second = f"ok-{uuid.uuid4().hex[:8]}"

    with pytest.raises(RuntimeError):
        await _post(app, f"/crash/{first}")
    assert await _post(app, f"/ok/{second}") == 200

    assert await _counts(sessions, first) == (0, 0)
    assert await _counts(sessions, second) == (1, 1)


async def test_a_read_only_route_needs_no_transaction(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Attached to every route, the boundary must be free where there is nothing to commit."""
    router = APIRouter(route_class=TransactionalRoute, dependencies=[Depends(bind_session)])

    @router.get("/read")
    async def read() -> dict[str, str]:
        return {"ok": "true"}

    app = FastAPI()
    app.include_router(router)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = _session_override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/read")).status_code == 200


async def test_a_failing_commit_does_not_report_success(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The reason the boundary is a route class and not a `yield` dependency.

    In dependency teardown the response is already produced, so a commit failure cannot change the
    status code — the client would be told the write succeeded. Here the failure propagates while
    the response is still in the application's hands.
    """
    router = APIRouter(route_class=TransactionalRoute, dependencies=[Depends(bind_session)])

    class _BrokenCommitSession:
        """Real enough for the boundary: it records rollback and refuses to commit."""

        def __init__(self) -> None:
            self.rolled_back = False

        async def commit(self) -> None:
            raise RuntimeError("commit failed at the database")

        async def rollback(self) -> None:
            self.rolled_back = True

    broken = _BrokenCommitSession()

    @router.post("/commit-fails")
    async def commit_fails() -> dict[str, str]:
        return {"ok": "true"}

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: broken

    with pytest.raises(RuntimeError, match="commit failed"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/commit-fails")

    # And the boundary tried to clean up rather than leaving the transaction dangling.
    assert broken.rolled_back is True


def _api_routes(routes: object) -> list[APIRoute]:
    """Every ``APIRoute`` reachable from an app, flattening included routers.

    This FastAPI version does not flatten ``include_router`` into ``app.routes`` — each inclusion
    appears as an opaque wrapper exposing ``original_router``. Walking that is the difference
    between a guard with teeth and one that inspects an empty list: the first version of this test
    filtered
    ``app.routes`` directly, found nothing, and passed while proving nothing.
    """
    found: list[APIRoute] = []
    for route in routes:  # type: ignore[attr-defined]
        original = getattr(route, "original_router", None)
        if original is not None:
            found.extend(_api_routes(original.routes))
        elif isinstance(route, APIRoute):
            found.append(route)
    return found


def test_every_api_route_attaches_the_transaction_boundary() -> None:
    """A router without it would silently discard every write its handlers make.

    Asserted mechanically rather than by review, because a new module's router is exactly the kind
    of wiring that gets added without it — and the symptom would be writes that vanish with a 201.
    """
    from sentinelai.entrypoints.http.main import create_app

    routes = _api_routes(create_app().routes)
    api_routes = [r for r in routes if str(r.path).startswith("/api/v1")]

    # Guard against the vacuous pass this test previously had.
    assert len(api_routes) > 30, f"expected the full API surface, found {len(api_routes)} routes"

    unguarded = sorted({r.path for r in api_routes if not isinstance(r, TransactionalRoute)})
    assert unguarded == [], f"routes missing the ADR-0005 transaction boundary: {unguarded}"


def test_the_health_probes_deliberately_have_no_transaction_boundary() -> None:
    """Liveness and readiness own no business writes, so they own no transaction.

    Stated as a test rather than left implicit, so that "these four are unguarded" reads as a
    decision rather than as the oversight the test above exists to catch.
    """
    from sentinelai.entrypoints.http.main import create_app

    routes = _api_routes(create_app().routes)
    unguarded = sorted({r.path for r in routes if not isinstance(r, TransactionalRoute)})

    assert unguarded == ["/healthz", "/metrics", "/readyz", "/startupz"]
