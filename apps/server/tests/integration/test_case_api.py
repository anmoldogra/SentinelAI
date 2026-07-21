"""API-level tests for case_management (guide Part 13 "API tests").

Exercises the real router → service path with a fake UoW, using
``app.dependency_overrides`` for auth + persistence (the documented testing seam).
No DB/Redis: ``ASGITransport`` does not run the lifespan.
"""

from __future__ import annotations

from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.case_management.service import CaseService, get_case_service
from sentinelai.platform.auth.dependencies import (
    CurrentUser,
    get_case_access_checker,
    get_current_user,
)


class _AllowAll:
    async def user_has_access(self, case_id: object, user_id: object) -> bool:
        return True


def _app_with_overrides(uow) -> object:
    app = create_app()
    user = CurrentUser(user_id=uuid4(), roles=("investigator",))
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_case_access_checker] = lambda: _AllowAll()
    app.dependency_overrides[get_case_service] = lambda: CaseService(uow)
    return app


async def test_create_then_get_case(uow) -> None:
    app = _app_with_overrides(uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/cases", json={"title": "Operation Nightfall"})
        assert created.status_code == 201
        body = created.json()
        assert body["data"]["status"] == "open"
        assert body["data"]["title"] == "Operation Nightfall"
        assert body["meta"]["request_id"]
        assert created.headers.get("ETag")

        case_id = body["data"]["case_id"]
        fetched = await client.get(f"/api/v1/cases/{case_id}")
        assert fetched.status_code == 200
        assert fetched.json()["data"]["case_id"] == case_id


async def test_malformed_create_returns_400(uow) -> None:
    app = _app_with_overrides(uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/cases", json={})  # missing required title
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_status_transition_via_api(uow) -> None:
    app = _app_with_overrides(uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = (await client.post("/api/v1/cases", json={"title": "X"})).json()["data"]
        resp = await client.post(
            f"/api/v1/cases/{created['case_id']}/status", json={"new_status": "closed"}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "closed"
