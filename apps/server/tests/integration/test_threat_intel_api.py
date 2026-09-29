"""The threat-intel HTTP surface — api-design.md §4.4.

The contract a feed integration programs against: the `Idempotency-Key` behaviour on the two "Yes
(key)" POSTs, the per-endpoint RBAC §4.4 specifies, and the `202` on an async feed sync.

Idempotency is asserted on a **call counter**, not on matching response bodies: "a retried IOC
submission does not register it twice" means the service was not re-entered, and identical bodies
alone would pass against an implementation that ran twice and discarded the second result.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.threat_intel.models import (
    FeedSubscription,
    Ioc,
    IocEvidenceMatch,
    ThreatActorProfile,
)
from sentinelai.modules.threat_intel.repository import STATUS_ACTIVE
from sentinelai.modules.threat_intel.schemas import FeedCreate, IocCreate, ThreatActorCreate
from sentinelai.modules.threat_intel.service import get_threat_intel_service
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.db.session import get_session
from sentinelai.platform.idempotency.guard import HEADER_NAME, REPLAY_HEADER
from sentinelai.platform.idempotency.models import IdempotencyKey
from tests.fixtures.kms import kms_for_tests

_KEY = "ti-idem-key-000000001"
_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_ADMIN = CurrentUser(user_id=uuid4(), roles=("admin",))
_INVESTIGATOR = CurrentUser(user_id=uuid4(), roles=("investigator",))
_SYSTEM = CurrentUser(user_id=uuid4(), roles=("system",))
_COMPLIANCE = CurrentUser(user_id=uuid4(), roles=("compliance",))


class _StubService:
    """Records calls and returns plausible rows, so re-entry is observable.

    The real service is exercised against Postgres in `test_threat_intel_db.py`; what this file
    needs to observe is how many times the router called it.
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.actor = ThreatActorProfile(
            threat_actor_id=uuid4(), name="APT-Example", aliases=None, description=None
        )
        self.feed = FeedSubscription(
            subscription_id=uuid4(),
            feed_name="vendor-x",
            protocol="taxii2",
            is_active=True,
            last_synced_at=None,
        )
        self.iocs: list[Ioc] = []

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def _ioc(self) -> Ioc:
        return Ioc(
            ioc_id=uuid4(),
            evidence_id=None,
            status=STATUS_ACTIVE,
            collected_at=_NOW,
            indicator_type="hash_sha256",
            value=_SHA256,
            threat_actor_id=None,
            first_seen=_NOW,
            last_seen=_NOW,
        )

    async def list_iocs(self, actor: CurrentUser, page: Any) -> list[Ioc]:
        self._count("list_iocs")
        return self.iocs

    async def register_ioc(self, data: IocCreate, actor: CurrentUser, correlation_id: str) -> Ioc:
        self._count("register_ioc")
        ioc = self._ioc()
        self.iocs.append(ioc)
        return ioc

    async def get_ioc(self, ioc_id: UUID, actor: CurrentUser) -> Ioc:
        self._count("get_ioc")
        return self._ioc()

    async def list_matches(
        self, ioc_id: UUID, actor: CurrentUser, page: Any = None
    ) -> list[IocEvidenceMatch]:
        self._count("list_matches")
        return [
            IocEvidenceMatch(
                match_id=uuid4(),
                ioc_id=ioc_id,
                matched_evidence_id=uuid4(),
                matched_at=_NOW,
                confidence=Decimal("1.000"),
            )
        ]

    async def list_threat_actors(self, actor: CurrentUser) -> list[ThreatActorProfile]:
        self._count("list_threat_actors")
        return [self.actor]

    async def create_threat_actor(
        self, data: ThreatActorCreate, actor: CurrentUser, correlation_id: str
    ) -> ThreatActorProfile:
        self._count("create_threat_actor")
        return self.actor

    async def list_feeds(self, actor: CurrentUser) -> list[FeedSubscription]:
        self._count("list_feeds")
        return [self.feed]

    async def add_feed(
        self, data: FeedCreate, actor: CurrentUser, correlation_id: str
    ) -> FeedSubscription:
        self._count("add_feed")
        return self.feed

    async def sync_feed(
        self, subscription_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> None:
        self._count("sync_feed")


class _IdempotencySession:
    """The slice of ``AsyncSession`` the idempotency repository uses, honouring rollback.

    Rollback matters: the claim shares the request's transaction, so "a failed submission leaves its
    key reusable" is a property of ROLLBACK, and a fake that kept the row would assert the opposite
    of production.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str, str], Any] = {}
        self._pending: list[tuple[UUID, str, str]] = []
        self.commits = 0

    def add(self, row: Any) -> None:
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
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_kms] = lambda: kms_for_tests()
    app.dependency_overrides[get_session] = lambda: store
    app.dependency_overrides[get_threat_intel_service] = lambda: service
    return app


@pytest.fixture
def service() -> _StubService:
    return _StubService()


@pytest.fixture
def store() -> _IdempotencySession:
    return _IdempotencySession()


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_IOC_BODY = {"indicator_type": "hash_sha256", "value": _SHA256}


# --- IOC registration -------------------------------------------------------
async def test_registering_an_ioc_returns_the_documented_shape(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§4.4: "Full IOC object, including `ioc_id`, `evidence_id: null` (not yet published)"."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post("/api/v1/threat-intel/iocs", json=_IOC_BODY)

    assert response.status_code == 201
    body = response.json()["data"]
    assert body["ioc_id"]
    assert body["evidence_id"] is None
    assert body["indicator_type"] == "hash_sha256"


async def test_a_retried_ioc_submission_replays_and_does_not_re_register(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§4.4 marks this endpoint "Idempotency: `Idempotency-Key` required". A feed retrying after a
    timeout must not register the same indicator twice — which would then match the same evidence
    twice and turn one sighting into two alerts."""
    async with await _client(_app(service, _SYSTEM, store)) as client:
        first = await client.post(
            "/api/v1/threat-intel/iocs", json=_IOC_BODY, headers={HEADER_NAME: _KEY}
        )
        second = await client.post(
            "/api/v1/threat-intel/iocs", json=_IOC_BODY, headers={HEADER_NAME: _KEY}
        )

    assert first.status_code == second.status_code == 201
    assert second.content == first.content
    assert second.headers[REPLAY_HEADER] == "true"
    assert service.calls["register_ioc"] == 1, "the service must not run a second time"


async def test_the_same_key_with_a_different_indicator_is_a_conflict(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§2.9: `409 IDEMPOTENCY_KEY_CONFLICT`. A feed reusing one key across two indicators is the
    bug the store exists to catch."""
    async with await _client(_app(service, _SYSTEM, store)) as client:
        await client.post("/api/v1/threat-intel/iocs", json=_IOC_BODY, headers={HEADER_NAME: _KEY})
        second = await client.post(
            "/api/v1/threat-intel/iocs",
            json={"indicator_type": "ipv4", "value": "192.0.2.10"},
            headers={HEADER_NAME: _KEY},
        )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert service.calls["register_ioc"] == 1


async def test_a_threat_actor_creation_is_idempotent_too(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§4.4 marks `POST /threat-actors` "Yes (key)" as well."""
    body = {"name": "APT-Example", "aliases": ["Group X"]}
    async with await _client(_app(service, _ADMIN, store)) as client:
        first = await client.post(
            "/api/v1/threat-intel/threat-actors", json=body, headers={HEADER_NAME: _KEY}
        )
        second = await client.post(
            "/api/v1/threat-intel/threat-actors", json=body, headers={HEADER_NAME: _KEY}
        )

    assert first.status_code == second.status_code == 201
    assert service.calls["create_threat_actor"] == 1


async def test_adding_a_feed_is_idempotent(
    service: _StubService, store: _IdempotencySession
) -> None:
    body = {"feed_name": "vendor-x", "protocol": "taxii2"}
    async with await _client(_app(service, _ADMIN, store)) as client:
        first = await client.post(
            "/api/v1/threat-intel/feeds", json=body, headers={HEADER_NAME: _KEY}
        )
        second = await client.post(
            "/api/v1/threat-intel/feeds", json=body, headers={HEADER_NAME: _KEY}
        )

    assert first.status_code == second.status_code == 201
    assert service.calls["add_feed"] == 1


async def test_a_duplicate_feed_sync_replays_rather_than_enqueuing_twice(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§4.4 marks the sync trigger "Idempotent? Yes".

    Two identical triggers must not queue two jobs: a feed pulled twice concurrently would race its
    own IOC upserts.
    """
    subscription_id = service.feed.subscription_id
    async with await _client(_app(service, _ADMIN, store)) as client:
        path = f"/api/v1/threat-intel/feeds/{subscription_id}/sync"
        first = await client.post(path, headers={HEADER_NAME: _KEY})
        second = await client.post(path, headers={HEADER_NAME: _KEY})

    assert first.status_code == second.status_code == 202
    assert second.headers[REPLAY_HEADER] == "true"
    assert service.calls["sync_feed"] == 1, "one trigger, one job"


async def test_two_submissions_without_keys_both_land(
    service: _StubService, store: _IdempotencySession
) -> None:
    """No key means no deduplication — the guard is a no-op without the header."""
    async with await _client(_app(service, _SYSTEM, store)) as client:
        await client.post("/api/v1/threat-intel/iocs", json=_IOC_BODY)
        await client.post("/api/v1/threat-intel/iocs", json=_IOC_BODY)

    assert service.calls["register_ioc"] == 2
    assert store.rows == {}


# --- reads ------------------------------------------------------------------
async def test_listing_matches_returns_the_documented_fields(
    service: _StubService, store: _IdempotencySession
) -> None:
    """§4.4: `{ match_id, evidence_id, matched_at, confidence }` — what an analyst needs to see
    where an indicator has turned up."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.get(f"/api/v1/threat-intel/iocs/{uuid4()}/matches")

    assert response.status_code == 200
    match = response.json()["data"][0]
    assert match["match_id"] and match["matched_evidence_id"]
    assert match["matched_at"]
    assert Decimal(str(match["confidence"])) == Decimal("1.000")


# --- RBAC -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "actor", "expected"),
    [
        # §4.4: IOC registration is investigator **or system**; a feed integration runs as
        # `system`.
        ("post", "/api/v1/threat-intel/iocs", _SYSTEM, 201),
        ("post", "/api/v1/threat-intel/iocs", _INVESTIGATOR, 201),
        ("post", "/api/v1/threat-intel/iocs", _COMPLIANCE, 403),
        # Threat actors: investigator or admin.
        ("post", "/api/v1/threat-intel/threat-actors", _ADMIN, 201),
        ("post", "/api/v1/threat-intel/threat-actors", _INVESTIGATOR, 201),
        ("post", "/api/v1/threat-intel/threat-actors", _SYSTEM, 403),
        # Feeds are admin-only: a subscription decides what enters the threat library.
        ("post", "/api/v1/threat-intel/feeds", _ADMIN, 201),
        ("post", "/api/v1/threat-intel/feeds", _INVESTIGATOR, 403),
        ("get", "/api/v1/threat-intel/feeds", _ADMIN, 200),
        ("get", "/api/v1/threat-intel/feeds", _INVESTIGATOR, 403),
        # IOC reads are investigator.
        ("get", "/api/v1/threat-intel/iocs", _INVESTIGATOR, 200),
        ("get", "/api/v1/threat-intel/iocs", _COMPLIANCE, 403),
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
    payloads = {
        "/api/v1/threat-intel/iocs": _IOC_BODY,
        "/api/v1/threat-intel/threat-actors": {"name": "APT-Example"},
        "/api/v1/threat-intel/feeds": {"feed_name": "vendor-x", "protocol": "taxii2"},
    }
    async with await _client(_app(service, actor, store)) as client:
        call = getattr(client, method)
        response = await (call(path, json=payloads[path]) if method == "post" else call(path))

    assert response.status_code == expected
