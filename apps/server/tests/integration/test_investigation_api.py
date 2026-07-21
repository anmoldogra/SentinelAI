"""API-level tests for investigation (real router → service, fake UoW, overrides)."""

from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.investigation.service import InvestigationService, get_investigation_service
from sentinelai.platform.auth.dependencies import CurrentUser, get_current_user


def _app_with_overrides(inv_uow) -> object:
    app = create_app()
    user = CurrentUser(user_id=uuid4(), roles=("investigator",))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_investigation_service] = lambda: InvestigationService(inv_uow)
    return app


async def test_create_entity_endpoint(inv_uow) -> None:
    app = _app_with_overrides(inv_uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/entities", json={"entity_type": "person", "canonical_name": "John Doe"}
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["data"]["status"] == "confirmed"
    assert body["data"]["canonical_name"] == "John Doe"
    assert body["meta"]["request_id"]
    assert resp.headers.get("ETag")


async def test_list_entities_endpoint(inv_uow) -> None:
    app = _app_with_overrides(inv_uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/v1/entities", json={"entity_type": "org", "canonical_name": "ACME"})
        listed = await client.get("/api/v1/entities")
    assert listed.status_code == 200
    assert len(listed.json()["data"]) >= 1
