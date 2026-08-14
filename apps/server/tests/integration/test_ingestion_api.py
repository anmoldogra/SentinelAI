"""API-level tests for ingestion (real router → service, fake UoW, overrides)."""

from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.ingestion.service import EvidenceService, get_evidence_service
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user


def _app(ing_uow) -> object:
    ing_uow.attribute_schemas.registered.add(("1.0.0", "osint", "web_page"))
    app = create_app()
    user = CurrentUser(user_id=uuid4(), roles=("investigator",))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_evidence_service] = lambda: EvidenceService(ing_uow)
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
    app.dependency_overrides[get_evidence_service] = lambda: EvidenceService(ing_uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/evidence", json=_BODY)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
