"""The OSINT connector surface over HTTP — api-design.md §4.3.

Covers what the DB-level suite cannot: the contract an external connector actually programs against.
The `Idempotency-Key` behaviour, the RBAC boundaries §4.3 specifies per endpoint, and the ETag guard
—
all through the real router stack with the real idempotency dependency (Wave 3.2) in the path.

The idempotency assertions check a **call counter on the service**, not just matching response
bodies:
"a retried push does not double-create a finding" means the service was not re-entered, and a
body-only assertion would pass against an implementation that ran twice and discarded the second
result.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.osint.exceptions import FindingNotFoundError
from sentinelai.modules.osint.models import OsintFinding, OsintSource
from sentinelai.modules.osint.schemas import FindingCreate, SourceCreate, SourceUpdate
from sentinelai.modules.osint.service import (
    STATUS_CAPTURED,
    STATUS_PUBLISHED,
    get_osint_service,
    source_etag,
)
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.idempotency.guard import HEADER_NAME, REPLAY_HEADER
from sentinelai.shared.exceptions import PreconditionFailedError
from tests.fixtures.kms import kms_for_tests

_KEY = "osint-idem-key-00000001"
_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

_ADMIN = CurrentUser(user_id=uuid4(), roles=("admin",))
_INVESTIGATOR = CurrentUser(user_id=uuid4(), roles=("investigator",))
_SYSTEM = CurrentUser(user_id=uuid4(), roles=("system",))
_COMPLIANCE = CurrentUser(user_id=uuid4(), roles=("compliance",))


class _StubService:
    """Records calls and returns plausible rows, so the tests can see re-entry.

    A stub rather than the real service: the real one is exercised end-to-end against Postgres in
    `test_osint_connector_db.py`, and what this file needs to observe is how many times the router
    called it — which a working implementation would hide behind identical results.
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.source = OsintSource(
            source_id=uuid4(),
            name="whois-connector",
            connector_type="api_pull",
            reliability_baseline="B2",
            is_active=True,
        )
        self.findings: list[OsintFinding] = []

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def _finding(self, *, published: bool = False) -> OsintFinding:
        return OsintFinding(
            finding_id=uuid4(),
            source_id=self.source.source_id,
            evidence_id=uuid4() if published else None,
            status=STATUS_PUBLISHED if published else STATUS_CAPTURED,
            collected_at=_NOW,
            raw_attributes={"attributes": {"domain": "suspect.example"}},
            reliability_rating="B2",
        )

    async def list_sources(self, actor: CurrentUser) -> list[OsintSource]:
        self._count("list_sources")
        return [self.source]

    async def register_source(
        self, data: SourceCreate, actor: CurrentUser, correlation_id: str
    ) -> OsintSource:
        self._count("register_source")
        return self.source

    async def update_source(
        self,
        source_id: UUID,
        data: SourceUpdate,
        actor: CurrentUser,
        expected_etag: str,
        correlation_id: str,
    ) -> OsintSource:
        self._count("update_source")
        if expected_etag != source_etag(self.source):
            raise PreconditionFailedError("source was modified concurrently (ETag mismatch)")
        return self.source

    async def list_findings(self, actor: CurrentUser, page: Any) -> list[OsintFinding]:
        self._count("list_findings")
        return self.findings

    async def get_finding(self, finding_id: UUID, actor: CurrentUser) -> OsintFinding:
        self._count("get_finding")
        for finding in self.findings:
            if finding.finding_id == finding_id:
                return finding
        raise FindingNotFoundError()

    async def create_finding(
        self, data: FindingCreate, actor: CurrentUser, correlation_id: str
    ) -> OsintFinding:
        self._count("create_finding")
        finding = self._finding()
        self.findings.append(finding)
        return finding

    async def publish_finding(
        self, finding_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> OsintFinding:
        self._count("publish_finding")
        return self._finding(published=True)


class _IdempotencySession:
    """The slice of ``AsyncSession`` the idempotency repository uses, honouring rollback.

    Modelling rollback is not optional: the claim shares the request's transaction, so "a failed
    push
    leaves its key reusable" is a property of ROLLBACK, and a fake that kept the row would assert
    the
    opposite of production.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str, str], Any] = {}
        self._pending: list[tuple[UUID, str, str]] = []
        self.commits = 0

    def add(self, row: Any) -> None:
        from sentinelai.platform.idempotency.models import IdempotencyKey

        if isinstance(row, IdempotencyKey):
            claim = (row.principal_id, row.idempotency_key, row.path)
            if claim in self.rows:
                from sqlalchemy.exc import IntegrityError

                raise IntegrityError("duplicate claim", None, Exception("uq_idempotency_claim"))
            self.rows[claim] = row
            self._pending.append(claim)

    async def flush(self) -> None:
        return None

    async def delete(self, row: Any) -> None:
        claim = (row.principal_id, row.idempotency_key, row.path)
        self.rows.pop(claim, None)
        if claim in self._pending:
            self._pending.remove(claim)

    async def execute(self, statement: Any) -> Any:
        return _Result(self, statement)

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()

    async def commit(self) -> None:
        self.commits += 1
        self._pending.clear()

    async def rollback(self) -> None:
        for claim in self._pending:
            self.rows.pop(claim, None)
        self._pending.clear()


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Result:
    def __init__(self, session: _IdempotencySession, statement: Any) -> None:
        self._session = session
        self._statement = statement

    def scalar_one_or_none(self) -> Any:
        found: dict[str, Any] = {}
        clauses = getattr(self._statement, "whereclause", None)
        for clause in getattr(clauses, "clauses", []):
            left, right = getattr(clause, "left", None), getattr(clause, "right", None)
            if left is not None and right is not None and hasattr(right, "value"):
                found[left.name] = right.value
        key = (found.get("principal_id"), found.get("idempotency_key"), found.get("path"))
        if None in key:
            return None
        return self._session.rows.get(key)  # type: ignore[arg-type]


def _app(service: _StubService, actor: CurrentUser, store: _IdempotencySession) -> Any:
    from sentinelai.platform.db.session import get_session

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_kms] = lambda: kms_for_tests()
    app.dependency_overrides[get_session] = lambda: store
    app.dependency_overrides[get_osint_service] = lambda: service
    return app


@pytest.fixture
def service() -> _StubService:
    return _StubService()


@pytest.fixture
def store() -> _IdempotencySession:
    return _IdempotencySession()


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _finding_body(source_id: UUID) -> dict[str, Any]:
    return {
        "source_id": str(source_id),
        "raw_attributes": {
            "schema_version": "1.0.0",
            "artifact_type": "domain_whois",
            "title": "WHOIS for suspect.example",
            "confidence": "0.9",
            "attributes": {"domain": "suspect.example"},
        },
    }


# --- the connector push -----------------------------------------------------
async def test_a_connector_can_push_a_finding(
    service: _StubService, store: _IdempotencySession
) -> None:
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post(
            "/api/v1/osint/findings", json=_finding_body(service.source.source_id)
        )

    assert response.status_code == 201
    body = response.json()["data"]
    assert body["status"] == STATUS_CAPTURED
    assert body["evidence_id"] is None, "§3.3: a finding is not evidence until published"
    assert service.calls["create_finding"] == 1


async def test_a_retried_push_replays_and_does_not_re_execute(
    service: _StubService, store: _IdempotencySession
) -> None:
    """Wave 3.2's store, on the endpoint §4.3 marks "Yes (key)".

    The counter is the assertion that matters: a connector retrying after a timeout must not create
    a
    second finding, and identical response bodies alone would not prove the service was not
    re-entered.
    """
    body = _finding_body(service.source.source_id)
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        first = await client.post("/api/v1/osint/findings", json=body, headers={HEADER_NAME: _KEY})
        second = await client.post("/api/v1/osint/findings", json=body, headers={HEADER_NAME: _KEY})

    assert first.status_code == second.status_code == 201
    assert second.content == first.content, "§2.9: the original response, replayed verbatim"
    assert second.headers[REPLAY_HEADER] == "true"
    assert service.calls["create_finding"] == 1, "the service must not run a second time"
    assert len(service.findings) == 1


async def test_the_same_key_with_a_different_payload_is_a_conflict(
    service: _StubService, store: _IdempotencySession
) -> None:
    """api-design.md §2.9: `409 IDEMPOTENCY_KEY_CONFLICT`. A connector reusing one key across two
    different findings is the bug the store exists to catch."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        first = await client.post(
            "/api/v1/osint/findings",
            json=_finding_body(service.source.source_id),
            headers={HEADER_NAME: _KEY},
        )
        altered = _finding_body(service.source.source_id)
        altered["raw_attributes"]["title"] = "A different record entirely"
        second = await client.post(
            "/api/v1/osint/findings", json=altered, headers={HEADER_NAME: _KEY}
        )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert service.calls["create_finding"] == 1


async def test_two_findings_without_keys_both_land(
    service: _StubService, store: _IdempotencySession
) -> None:
    """No key means no deduplication — the guard is a no-op without the header, so nothing changes
    for a connector that does not send one."""
    body = _finding_body(service.source.source_id)
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        await client.post("/api/v1/osint/findings", json=body)
        await client.post("/api/v1/osint/findings", json=body)

    assert service.calls["create_finding"] == 2
    assert store.rows == {}


# --- publishing -------------------------------------------------------------
async def test_publishing_returns_the_evidence_id(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§4.3's response body carries `finding_id`, `evidence_id` and `status: "published"` — the
    synchronous outcome that makes this a call into `ingestion` rather than an outbox hand-off."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post(f"/api/v1/osint/findings/{uuid4()}/publish")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == STATUS_PUBLISHED
    assert body["evidence_id"] is not None
    assert body["finding_id"]


async def test_a_system_actor_may_publish(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§4.3 grants publish to `investigator` **or** `system`: an automated connector pipeline
    publishes its own findings without a human in the loop."""
    async with await _client(_app(service, _SYSTEM, store)) as client:
        response = await client.post(f"/api/v1/osint/findings/{uuid4()}/publish")
    assert response.status_code == 200


# --- RBAC -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "actor", "expected"),
    [
        # §4.3: registering a source is admin-only — it defines provenance every finding inherits.
        ("post", "/api/v1/osint/sources", _INVESTIGATOR, 403),
        ("post", "/api/v1/osint/sources", _ADMIN, 201),
        # Findings are captured by investigators, not admins.
        ("post", "/api/v1/osint/findings", _ADMIN, 403),
        ("post", "/api/v1/osint/findings", _INVESTIGATOR, 201),
        # Compliance reads the audit trail, not the OSINT intake surface.
        ("get", "/api/v1/osint/findings", _COMPLIANCE, 403),
        ("get", "/api/v1/osint/findings", _INVESTIGATOR, 200),
        ("get", "/api/v1/osint/sources", _ADMIN, 200),
        ("get", "/api/v1/osint/sources", _INVESTIGATOR, 200),
    ],
)
async def test_rbac_matches_the_documented_roles(
    service: _StubService,
    store: _IdempotencySession,
    method: str,
    path: str,
    actor: CurrentUser,
    expected: int,
) -> None:
    payload = (
        {"name": "f", "connector_type": "api_pull"}
        if path.endswith("/sources")
        else _finding_body(service.source.source_id)
    )
    async with await _client(_app(service, actor, store)) as client:
        call = getattr(client, method)
        response = await (call(path, json=payload) if method == "post" else call(path))

    assert response.status_code == expected


# --- ETag -------------------------------------------------------------------
async def test_updating_a_source_requires_a_matching_if_match(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§4.3 marks the source PATCH "No (ETag)" — optimistic concurrency rather than an idempotency
    key, because two operators editing one feed's reliability must not silently overwrite each
    other."""
    source_id = service.source.source_id
    async with await _client(_app(service, _ADMIN, store)) as client:
        ok = await client.patch(
            f"/api/v1/osint/sources/{source_id}",
            json={"reliability_baseline": "C3"},
            headers={"If-Match": source_etag(service.source)},
        )
        stale = await client.patch(
            f"/api/v1/osint/sources/{source_id}",
            json={"reliability_baseline": "D4"},
            headers={"If-Match": 'W/"0000000000000000"'},
        )

    assert ok.status_code == 200
    assert stale.status_code == 412


async def test_the_source_patch_requires_if_match_at_all(
    service: _StubService, store: _IdempotencySession
) -> None:
    """The header is declared required, so omitting it is a 400 rather than an unguarded write."""
    async with await _client(_app(service, _ADMIN, store)) as client:
        response = await client.patch(
            f"/api/v1/osint/sources/{service.source.source_id}",
            json={"reliability_baseline": "C3"},
        )

    assert response.status_code == 400
    assert service.calls.get("update_source", 0) == 0
