"""The forensics HTTP surface — api-design.md §4.5.

The contract an examiner's client programs against: the `Idempotency-Key` behaviour on the two
"Yes" endpoints, §4.5's per-endpoint RBAC, the `201`/`200` shapes, and the `422`s a malformed
artifact definition earns.

Idempotency is asserted on a **call counter**, not on matching response bodies: "a retried
registration does not register the acquisition twice" means the service was not re-entered, and
identical bodies alone would pass against an implementation that ran twice and discarded the second
result. Registering one seizure twice would put two artifact records in a case file for one act.

The real service is exercised against Postgres in `test_forensics_db.py`; what this file observes is
what the *router* does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.forensics.exceptions import ArtifactNotFoundError
from sentinelai.modules.forensics.models import Artifact
from sentinelai.modules.forensics.schemas import ArtifactCreate
from sentinelai.modules.forensics.service import (
    STATUS_PUBLISHED,
    STATUS_REGISTERED,
    category_for_kind,
    get_forensics_service,
)
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user
from sentinelai.platform.crypto import get_kms
from sentinelai.platform.db.session import get_session
from sentinelai.platform.idempotency.guard import HEADER_NAME, REPLAY_HEADER
from sentinelai.shared.exceptions import ValidationFailedError
from tests.fixtures.idempotency import IdempotencySession
from tests.fixtures.kms import kms_for_tests

_KEY = "forensics-idem-key-00001"
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_ADMIN = CurrentUser(user_id=uuid4(), roles=("admin",))
_INVESTIGATOR = CurrentUser(user_id=uuid4(), roles=("investigator",))
_SYSTEM = CurrentUser(user_id=uuid4(), roles=("system",))

_BODY = {
    "artifact_kind": "forensic_image",
    "acquisition_tool": "EnCase 8",
    "acquisition_hash": f"SHA-256:{_SHA256}",
    "collected_at": _NOW.isoformat(),
    "device_info": {"serial": "WD-WX21A", "capacity_bytes": 512110190592},
}


class _StubService:
    """Records calls and returns plausible rows, so re-entry is observable."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.artifacts: list[Artifact] = []

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def _artifact(self, *, published: bool = False) -> Artifact:
        return Artifact(
            artifact_id=uuid4(),
            evidence_id=uuid4() if published else None,
            status=STATUS_PUBLISHED if published else STATUS_REGISTERED,
            collected_at=_NOW,
            artifact_kind="forensic_image",
            device_info={"serial": "WD-WX21A"},
            acquisition_tool="EnCase 8",
            acquisition_hash=f"SHA-256:{_SHA256}",
        )

    async def list_artifacts(
        self, actor: CurrentUser, page: Any
    ) -> tuple[list[Artifact], str | None, bool]:
        self._count("list_artifacts")
        return self.artifacts, None, False

    async def register_artifact(
        self, data: ArtifactCreate, actor: CurrentUser, correlation_id: str
    ) -> Artifact:
        self._count("register_artifact")
        # The router hands the parsed body straight through, so validating here is what proves the
        # route did not quietly normalize it.
        category_for_kind(data.artifact_kind)
        artifact = self._artifact()
        self.artifacts.append(artifact)
        return artifact

    async def get_artifact(self, artifact_id: UUID, actor: CurrentUser) -> Artifact:
        self._count("get_artifact")
        if self.artifacts:
            return self.artifacts[0]
        raise ArtifactNotFoundError()

    async def publish_artifact(
        self, artifact_id: UUID, actor: CurrentUser, correlation_id: str
    ) -> Artifact:
        self._count("publish_artifact")
        return self._artifact(published=True)


def _app(service: _StubService, actor: CurrentUser, store: IdempotencySession) -> Any:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_kms] = lambda: kms_for_tests()
    app.dependency_overrides[get_session] = lambda: store
    app.dependency_overrides[get_forensics_service] = lambda: service
    return app


@pytest.fixture
def service() -> _StubService:
    return _StubService()


@pytest.fixture
def store() -> IdempotencySession:
    return IdempotencySession()


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- registration -----------------------------------------------------------
async def test_registering_an_artifact_returns_the_documented_shape(
    service: _StubService, store: IdempotencySession
) -> None:
    """§4.5: "Full artifact record, `evidence_id: null`", `201`."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post("/api/v1/forensics/artifacts", json=_BODY)

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["evidence_id"] is None
    assert data["status"] == STATUS_REGISTERED
    assert data["artifact_kind"] == "forensic_image"
    assert data["acquisition_tool"] == "EnCase 8"
    assert response.json()["meta"]["request_id"]


async def test_a_retried_registration_replays_instead_of_registering_twice(
    service: _StubService, store: IdempotencySession
) -> None:
    """§4.5 marks this endpoint "Yes (key)". Two artifact rows for one acquisition would be two
    records of one seizure in a case file, which is why the counter is what is asserted."""
    headers = {HEADER_NAME: _KEY}
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        first = await client.post("/api/v1/forensics/artifacts", json=_BODY, headers=headers)
        second = await client.post("/api/v1/forensics/artifacts", json=_BODY, headers=headers)

    assert first.status_code == second.status_code == 201
    assert service.calls["register_artifact"] == 1, "the service was not re-entered"
    assert second.headers.get(REPLAY_HEADER) == "true"
    assert second.json()["data"] == first.json()["data"]


async def test_a_different_key_registers_a_second_artifact(
    service: _StubService, store: IdempotencySession
) -> None:
    """Two genuine acquisitions are two records — the key scopes the replay, not the endpoint."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        await client.post(
            "/api/v1/forensics/artifacts", json=_BODY, headers={HEADER_NAME: f"{_KEY}-a"}
        )
        await client.post(
            "/api/v1/forensics/artifacts", json=_BODY, headers={HEADER_NAME: f"{_KEY}-b"}
        )

    assert service.calls["register_artifact"] == 2


async def test_a_rejected_registration_leaves_its_key_reusable(
    service: _StubService, store: IdempotencySession
) -> None:
    """The claim shares the request's transaction, so a `422` rolls it back.

    An examiner who mistyped a hash must be able to retry the same request with the same key once
    they fix it — a key burned by a rejection would make the client invent a new one to recover,
    which is exactly the confusion idempotency keys exist to remove.
    """
    bad = {**_BODY, "artifact_kind": "disk-image"}
    headers = {HEADER_NAME: _KEY}

    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        rejected = await client.post("/api/v1/forensics/artifacts", json=bad, headers=headers)
        retried = await client.post("/api/v1/forensics/artifacts", json=_BODY, headers=headers)

    assert rejected.status_code == 422
    assert retried.status_code == 201
    assert retried.headers.get(REPLAY_HEADER) is None


# --- validation -------------------------------------------------------------
@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"artifact_kind": "disk-image"}, "artifact_kind"),
        ({"artifact_kind": "post"}, "artifact_kind"),
    ],
    ids=["unknown-kind", "another-category"],
)
async def test_a_malformed_definition_is_a_422_naming_the_field(
    service: _StubService, store: IdempotencySession, override: dict[str, str], field: str
) -> None:
    """§4.5's error codes include `422`, and FR-1.3 wants the failed rule named per field."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post("/api/v1/forensics/artifacts", json={**_BODY, **override})

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "VALIDATION_FAILED"
    assert any(detail["field"] == field for detail in body["details"])


@pytest.mark.parametrize(
    "missing", ["artifact_kind", "acquisition_tool", "acquisition_hash", "collected_at"]
)
async def test_a_required_field_is_a_400_not_a_422(
    service: _StubService, store: IdempotencySession, missing: str
) -> None:
    """All four are required by §4.5's body, and an absent one is **400**, not 422.

    §2.4 draws that line explicitly: `VALIDATION_FAILED` is "400 or 422 — request malformed (400)
    or fails domain/business rules (422)". A body missing a field never reached a domain rule, so it
    is malformed; an `artifact_kind` outside CEM §6 parsed fine and then broke a rule, so it is 422.
    Asserted here because it is the kind of distinction that drifts endpoint by endpoint.

    `acquisition_tool` is in the required set for a reason beyond §4.5 listing it: it becomes
    `source.system`, without which CEM §13 rejects the evidence at publish time — so an artifact
    registered without one could never be published.
    """
    body = {key: value for key, value in _BODY.items() if key != missing}

    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post("/api/v1/forensics/artifacts", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_a_bad_acquisition_hash_is_a_422(
    service: _StubService, store: IdempotencySession
) -> None:
    """The service raises; this asserts the router surfaces it as §2.4's envelope rather than a 500.

    The stub validates the kind but not the hash, so the rejection is injected — what is under test
    is the error path, and the real hash rules are covered in `test_forensics_mapping.py`.
    """

    async def _refuse(data: ArtifactCreate, actor: CurrentUser, correlation_id: str) -> Artifact:
        raise ValidationFailedError(
            [{"field": "acquisition_hash", "message": "must be '<ALGORITHM>:<hexdigest>'"}]
        )

    service.register_artifact = _refuse  # type: ignore[method-assign]

    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post(
            "/api/v1/forensics/artifacts", json={**_BODY, "acquisition_hash": _SHA256}
        )

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "acquisition_hash"


# --- reads and publish ------------------------------------------------------
async def test_listing_returns_the_paginated_envelope(
    service: _StubService, store: IdempotencySession
) -> None:
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.get("/api/v1/forensics/artifacts")

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == []
    assert body["pagination"] == {"next_cursor": None, "has_more": False, "limit": 50}


async def test_getting_an_absent_artifact_is_a_404(
    service: _StubService, store: IdempotencySession
) -> None:
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.get(f"/api/v1/forensics/artifacts/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_publishing_returns_the_artifact_carrying_its_evidence_id(
    service: _StubService, store: IdempotencySession
) -> None:
    """§4.5: publication normalizes into `ingestion.evidence`, and the record now points at it."""
    async with await _client(_app(service, _INVESTIGATOR, store)) as client:
        response = await client.post(f"/api/v1/forensics/artifacts/{uuid4()}/publish")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["evidence_id"] is not None
    assert data["status"] == STATUS_PUBLISHED


# --- RBAC -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "actor", "expected"),
    [
        ("post", "/api/v1/forensics/artifacts", _INVESTIGATOR, 201),
        ("post", "/api/v1/forensics/artifacts", _SYSTEM, 201),
        ("post", "/api/v1/forensics/artifacts", _ADMIN, 403),
        ("get", "/api/v1/forensics/artifacts", _INVESTIGATOR, 200),
        ("get", "/api/v1/forensics/artifacts", _SYSTEM, 403),
    ],
)
async def test_rbac_matches_the_documented_roles(
    service: _StubService,
    store: IdempotencySession,
    method: str,
    path: str,
    actor: CurrentUser,
    expected: int,
) -> None:
    """§4.5's Auth column, asserted per endpoint.

    The two interesting rows are the negatives. `admin` cannot register an artifact: administration
    is not examination, and §4.5 names `investigator, system` — a platform administrator in a chain
    of custody would be a finding, not a convenience. And an automated `system` account may
    register acquisitions (a tool pushing its output) but not browse the artifact list, which §4.5
    keeps investigator-facing.
    """
    async with await _client(_app(service, actor, store)) as client:
        response = await (client.post(path, json=_BODY) if method == "post" else client.get(path))

    assert response.status_code == expected
