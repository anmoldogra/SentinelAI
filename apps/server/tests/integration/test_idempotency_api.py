"""End-to-end idempotency over the real router stack — ADR-0012, api-design.md §2.9.

The claim these tests have to substantiate is not "duplicate writes are deduplicated" but
**"the business logic does not run twice"**. So the service is wrapped in a counting spy and every
replay assertion checks that counter, not just the response body: an implementation that re-ran the
handler and discarded the second result would satisfy a body-only test and still double-ingest
evidence the moment a side effect escaped the transaction.

Runs against the real `create_app()` routers with a fake UoW and an in-memory idempotency store, so
the dependency, the pre-commit hook, the exception handler and the ADR-0005 boundary are all the
production ones. The store's *SQL* behaviour — the unique constraint that serializes a concurrent
duplicate — is proven separately against Postgres in `test_idempotency_db.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.case_management.service import CaseService, get_case_service
from sentinelai.platform.auth.dependencies import (
    CurrentUser,
    get_case_access_checker,
    get_current_user,
)
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.db.session import get_session
from sentinelai.platform.idempotency.guard import HEADER_NAME, REPLAY_HEADER
from sentinelai.platform.idempotency.models import (
    STATE_COMPLETED,
    IdempotencyKey,
)
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests

_KEY = "idem-key-0000000000000001"


class _AllowAll:
    async def user_has_access(self, case_id: object, user_id: object) -> bool:
        return True


class _FakeIdempotencySession:
    """The slice of ``AsyncSession`` the idempotency repository actually uses.

    A dict-backed store rather than a mock: the guard's logic depends on *finding* or *not finding*
    a row and on the unique claim tuple, and a mock that returned whatever it was told would prove
    only that the test author knew the answer.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str, str], IdempotencyKey] = {}
        # Rows written since the last commit. Modelling this is not optional: the claim lives in
        # the request's own transaction, so "a failed request leaves no claim" is a property of
        # ROLLBACK. A fake that kept the row would quietly assert the opposite of production.
        self._pending: list[tuple[UUID, str, str]] = []
        self.commits = 0
        self.rollbacks = 0

    # -- the repository's surface ------------------------------------------
    def add(self, row: object) -> None:
        if isinstance(row, IdempotencyKey):
            claim = (row.principal_id, row.idempotency_key, row.path)
            if claim in self.rows:
                from sqlalchemy.exc import IntegrityError

                raise IntegrityError("duplicate claim", None, Exception("uq_idempotency_claim"))
            self.rows[claim] = row
            self._pending.append(claim)

    async def flush(self) -> None:
        return None

    async def delete(self, row: object) -> None:
        if isinstance(row, IdempotencyKey):
            claim = (row.principal_id, row.idempotency_key, row.path)
            self.rows.pop(claim, None)
            if claim in self._pending:
                self._pending.remove(claim)

    async def execute(self, statement: Any) -> Any:
        return _FakeResult(self, statement)

    def begin_nested(self) -> _FakeSavepoint:
        return _FakeSavepoint()

    async def commit(self) -> None:
        self.commits += 1
        self._pending.clear()

    async def rollback(self) -> None:
        self.rollbacks += 1
        for claim in self._pending:
            self.rows.pop(claim, None)
        self._pending.clear()


class _FakeSavepoint:
    async def __aenter__(self) -> _FakeSavepoint:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeResult:
    """Answers the two statements the repository issues: the claim SELECT and the replay UPDATE."""

    def __init__(self, session: _FakeIdempotencySession, statement: Any) -> None:
        self._session = session
        self._statement = statement

    def scalar_one_or_none(self) -> IdempotencyKey | None:
        criteria = _criteria(self._statement)
        principal = criteria.get("principal_id")
        key = criteria.get("idempotency_key")
        path = criteria.get("path")
        if principal is None or key is None or path is None:
            return None
        return self._session.rows.get((principal, key, path))


def _criteria(statement: Any) -> dict[str, Any]:
    """Pull the equality-compared literals out of a SELECT's WHERE clause."""
    found: dict[str, Any] = {}
    for clause in statement.whereclause.clauses if hasattr(statement, "whereclause") else []:
        left = getattr(clause, "left", None)
        right = getattr(clause, "right", None)
        if left is not None and right is not None and hasattr(right, "value"):
            found[left.name] = right.value
    return found


class _CountingCaseService(CaseService):
    """A real service that counts how many times each mutating method actually executed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: dict[str, int] = {}

    async def create_case(self, *args: Any, **kwargs: Any) -> Any:
        self.calls["create_case"] = self.calls.get("create_case", 0) + 1
        return await super().create_case(*args, **kwargs)


def _app(uow: object, session: _FakeIdempotencySession, service: CaseService) -> Any:
    app = create_app()
    user = CurrentUser(user_id=uuid4(), roles=("investigator",))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_kms] = lambda: kms_for_tests()
    app.dependency_overrides[get_case_access_checker] = lambda: _AllowAll()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_case_service] = lambda: service
    return app


@pytest.fixture
def store() -> _FakeIdempotencySession:
    return _FakeIdempotencySession()


@pytest.fixture
def service(uow) -> _CountingCaseService:
    return _CountingCaseService(uow, storage=FakeObjectStorage(), kms=kms_for_tests())


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- replay -----------------------------------------------------------------
async def test_a_repeated_request_replays_without_re_executing(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """ADR-0012 §2(a). The counter is the assertion that matters."""
    app = _app(uow, store, service)
    body = {"title": "Operation Nightfall"}

    async with await _client(app) as client:
        first = await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})
        second = await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json(), "§2.9: the original response, replayed verbatim"
    assert service.calls["create_case"] == 1, (
        "the handler must not run again — deduplicating its writes afterwards is not the same thing"
    )


async def test_the_replayed_body_is_byte_identical(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """§2.9 says "same body", and a client that hashed or signed the original would notice a
    re-serialization that reordered keys."""
    app = _app(uow, store, service)
    body = {"title": "Byte Identical"}

    async with await _client(app) as client:
        first = await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})
        second = await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})

    assert second.content == first.content


async def test_a_replay_is_marked_as_one(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """Not in §2.9 — added because a replay indistinguishable from a fresh execution makes
    "did my retry take effect?" unanswerable from the wire."""
    app = _app(uow, store, service)
    body = {"title": "Marked"}

    async with await _client(app) as client:
        first = await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})
        second = await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})

    assert REPLAY_HEADER not in first.headers
    assert second.headers[REPLAY_HEADER] == "true"


async def test_three_retries_all_replay_the_first_answer(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """A flaky connector does not retry exactly once."""
    app = _app(uow, store, service)
    body = {"title": "Persistent"}

    async with await _client(app) as client:
        responses = [
            await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})
            for _ in range(4)
        ]

    assert {r.status_code for r in responses} == {201}
    assert len({r.content for r in responses}) == 1
    assert service.calls["create_case"] == 1


# --- conflict ---------------------------------------------------------------
async def test_the_same_key_with_a_different_body_is_a_conflict(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """api-design.md §2.9: `409 IDEMPOTENCY_KEY_CONFLICT`.

    409, not the 422 ADR-0012 §2(b) proposed: the entity is well-formed, and it is the *key reuse*
    that conflicts. §2.4's error table has said 409 since the API was designed.
    """
    app = _app(uow, store, service)

    async with await _client(app) as client:
        first = await client.post(
            "/api/v1/cases", json={"title": "First"}, headers={HEADER_NAME: _KEY}
        )
        second = await client.post(
            "/api/v1/cases", json={"title": "Second"}, headers={HEADER_NAME: _KEY}
        )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert service.calls["create_case"] == 1, "a conflicting request must not execute"


async def test_a_conflict_does_not_disturb_the_stored_response(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """The original must still be replayable after someone misuses its key — otherwise one buggy
    client could invalidate another request's cached answer."""
    app = _app(uow, store, service)

    async with await _client(app) as client:
        first = await client.post(
            "/api/v1/cases", json={"title": "Original"}, headers={HEADER_NAME: _KEY}
        )
        await client.post("/api/v1/cases", json={"title": "Other"}, headers={HEADER_NAME: _KEY})
        third = await client.post(
            "/api/v1/cases", json={"title": "Original"}, headers={HEADER_NAME: _KEY}
        )

    assert third.status_code == 201
    assert third.content == first.content


# --- scope ------------------------------------------------------------------
async def test_a_request_without_a_key_is_untouched(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """The guard is a no-op without the header, so attaching it to a router changes nothing for
    the endpoints §2.9 does not cover."""
    app = _app(uow, store, service)
    body = {"title": "Unkeyed"}

    async with await _client(app) as client:
        first = await client.post("/api/v1/cases", json=body)
        second = await client.post("/api/v1/cases", json=body)

    assert first.status_code == second.status_code == 201
    assert service.calls["create_case"] == 2, "no key means no deduplication"
    assert store.rows == {}, "and no row written"


async def test_a_safe_method_is_untouched(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """A `GET` carrying the header must not claim a key — it has no effect to deduplicate, and
    caching one would make the store grow on read traffic."""
    app = _app(uow, store, service)

    async with await _client(app) as client:
        response = await client.get("/api/v1/cases", headers={HEADER_NAME: _KEY})

    assert response.status_code == 200
    assert store.rows == {}


async def test_two_paths_do_not_share_a_key(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """Claims are scoped by path, so one key used against two endpoints is two claims — not a
    conflict, and not a cross-endpoint replay."""
    app = _app(uow, store, service)

    async with await _client(app) as client:
        created = await client.post(
            "/api/v1/cases", json={"title": "Scoped"}, headers={HEADER_NAME: _KEY}
        )
        case_id = created.json()["data"]["case_id"]
        other = await client.post(
            f"/api/v1/cases/{case_id}/status",
            json={"new_status": "closed"},
            headers={HEADER_NAME: _KEY},
        )

    assert created.status_code == 201
    assert other.status_code == 200, "a different path is a different claim, not a conflict"


@pytest.mark.parametrize("key", ["", "short", "x" * 256])
async def test_an_unusable_key_is_rejected(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService, key: str
) -> None:
    """A key too short to be unguessable, or long enough to be a payload, is a client bug worth
    reporting rather than storing."""
    app = _app(uow, store, service)

    async with await _client(app) as client:
        response = await client.post(
            "/api/v1/cases", json={"title": "Bad key"}, headers={HEADER_NAME: key}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert service.calls.get("create_case", 0) == 0


# --- failure and expiry -----------------------------------------------------
async def test_a_failed_request_leaves_no_claim(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """The claim shares the request's transaction, so a failure takes it down — which is what lets
    the client fix the problem and retry with the same key.

    Without this a transient failure would become permanent for the length of the TTL.
    """
    app = _app(uow, store, service)

    async with await _client(app) as client:
        rejected = await client.post(
            "/api/v1/cases", json={"title": ""}, headers={HEADER_NAME: _KEY}
        )
        assert rejected.status_code in (400, 422)

        # A 400 comes from shape validation, so the handler never ran and neither did the
        # pre-commit hook — the claim is cleared by ADR-0005's rollback, nothing else.
        assert store.rows == {}, "a rolled-back request must not leave its key claimed"
        assert store.rollbacks >= 1

        retried = await client.post(
            "/api/v1/cases", json={"title": "Now valid"}, headers={HEADER_NAME: _KEY}
        )

    assert retried.status_code == 201


async def test_an_expired_key_is_reusable(
    uow, store: _FakeIdempotencySession, service: _CountingCaseService
) -> None:
    """Past the 24h window there is nothing to replay, so the key is free again — and the guard
    clears the stale row itself rather than waiting for the nightly sweep, because the unique
    constraint would otherwise refuse the new claim."""
    app = _app(uow, store, service)
    body = {"title": "Expiring"}

    async with await _client(app) as client:
        first = await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})

        (row,) = store.rows.values()
        assert row.state == STATE_COMPLETED
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

        second = await client.post("/api/v1/cases", json=body, headers={HEADER_NAME: _KEY})

    assert first.status_code == second.status_code == 201
    assert second.content != first.content, "a fresh execution, not a replay"
    assert service.calls["create_case"] == 2
