"""API-level tests for ingestion (real router → service, fake UoW, overrides)."""

from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.ingestion.service import EvidenceService, get_evidence_service
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user
from tests.fixtures.fake_object_storage import FakeObjectStorage
from tests.fixtures.kms import kms_for_tests


def _app(ing_uow) -> object:
    ing_uow.attribute_schemas.registered.add(("1.0.0", "osint", "web_page"))
    app = create_app()
    user = CurrentUser(user_id=uuid4(), roles=("investigator",))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_evidence_service] = lambda: EvidenceService(
        ing_uow, storage=FakeObjectStorage(), kms=kms_for_tests()
    )
    return app


_BODY = {
    "schema_version": "1.0.0",
    "category": "osint",
    "artifact_type": "web_page",
    "title": "A captured post",
    "source": {"system": "connector-x", "collector_id": "c1"},
    "collected_at": "2026-01-01T00:00:00Z",
    "attributes": {},
    "confidence": 0.8,
    "inline_payload": {"k": "v"},
}


async def test_ingest_evidence_endpoint(ing_uow) -> None:
    app = _app(ing_uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/evidence", json=_BODY)
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["status"] == "validated"
    assert body["data"]["category"] == "osint"
    assert body["meta"]["request_id"]


async def test_ingest_unregistered_schema_returns_422(ing_uow) -> None:
    app = create_app()  # registry NOT seeded
    user = CurrentUser(user_id=uuid4(), roles=("investigator",))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_evidence_service] = lambda: EvidenceService(
        ing_uow, storage=FakeObjectStorage(), kms=kms_for_tests()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/evidence", json=_BODY)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_verify_chain_endpoint_returns_a_report_for_an_intact_chain(ing_uow) -> None:
    """`GET /evidence/{id}/verify` — ADR-0003 §6(a), the court-facing report.

    Goes through the real router and the real service, so it also pins the wire contract: the
    three-state `state` field and the per-entry findings a client must render distinctly.
    """
    app = _app(ing_uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/evidence", json=_BODY)
        evidence_id = created.json()["data"]["evidence_id"]
        resp = await client.get(f"/api/v1/evidence/{evidence_id}/verify")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ledger"] == "ingestion.evidence_custody_events"
    assert data["state"] == "verified"
    # Ingest wrote a genesis custody entry, and it must verify against its own signature.
    assert data["entry_count"] >= 1
    assert data["failed_entries"] == 0
    assert data["entries"][0]["state"] == "verified"
    assert data["entries"][0]["sequence"] == 1
    assert data["findings"] == []


async def test_verify_chain_endpoint_reports_failure_as_200_with_a_failed_verdict(
    ing_uow,
) -> None:
    """A broken chain is a successful request with bad news, not an HTTP error.

    An error status would leave a client unable to tell "this chain is broken" from "verification
    could not run" — opposite conclusions for anyone deciding whether a case is still prosecutable.
    """
    app = _app(ing_uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/evidence", json=_BODY)
        evidence_id = created.json()["data"]["evidence_id"]
        # Tamper with the stored entry the way a database-level attacker would.
        ing_uow.custody.items[0].actor_role = "admin"
        resp = await client.get(f"/api/v1/evidence/{evidence_id}/verify")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["state"] == "failed"
    assert data["failed_entries"] == 1
    assert "hash_mismatch" in data["findings"]
