"""API-level tests for case_management (guide Part 13 "API tests").

Exercises the real router → service path with a fake UoW, using
``app.dependency_overrides`` for auth + persistence (the documented testing seam).
No DB/Redis: ``ASGITransport`` does not run the lifespan.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from sentinelai.entrypoints.http.main import create_app
from sentinelai.modules.case_management.service import CaseService, get_case_service
from sentinelai.platform.auth.dependencies import (
    CurrentUser,
    get_case_access_checker,
    get_current_user,
)
from sentinelai.platform.tasks import get_task_queue
from tests.fixtures.fake_object_storage import FakeObjectStorage


class _AllowAll:
    async def user_has_access(self, case_id: object, user_id: object) -> bool:
        return True


class _RecordingTaskQueue:
    """Stands in for the arq pool: records enqueues instead of reaching Redis.

    ``get_task_queue`` reads ``app.state.task_queue``, which the HTTP lifespan populates —
    and ``ASGITransport`` does not run the lifespan, so it must be overridden here.
    """

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, tuple[object, ...]]] = []

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> object:
        self.enqueued.append((function, args))
        return None


def _app_with_overrides(uow, storage=None, task_queue=None) -> object:
    """Wire the real routers to fake persistence.

    ``storage`` is accepted so a test can share ONE object store between the request path and a
    simulated worker run; each call otherwise gets its own, as before.
    """
    app = create_app()
    user = CurrentUser(user_id=uuid4(), roles=("investigator",))
    shared_storage = storage if storage is not None else FakeObjectStorage()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_case_access_checker] = lambda: _AllowAll()
    app.dependency_overrides[get_case_service] = lambda: CaseService(uow, storage=shared_storage)
    if task_queue is not None:
        app.dependency_overrides[get_task_queue] = lambda: task_queue
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


async def test_report_lifecycle_api_flow(uow) -> None:
    """The full report lifecycle as an API client experiences it (api-design.md §7).

    request -> poll (queued) -> premature download refused -> [worker] -> poll (completed) ->
    download. This is the seam the unit suites cannot reach: each step is proven in isolation
    elsewhere, but only here do the 202's ``Location`` header, the polled ``status`` field, and
    the download gate have to agree with one another over HTTP.

    The worker is simulated by calling ``CaseService.complete_report`` directly — the exact
    method ``generate_case_report`` delegates to — against the same UoW and the same object
    store the API is using. That covers the state transition the client observes; it does not
    cover the job wrapper's own transaction/retry/failure handling, which has its own unit tests.
    """
    storage = FakeObjectStorage()
    queue = _RecordingTaskQueue()
    app = _app_with_overrides(uow, storage=storage, task_queue=queue)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        case_id = (await client.post("/api/v1/cases", json={"title": "Op Report"})).json()["data"][
            "case_id"
        ]

        # 1. Trigger generation -> 202 + a pollable id and Location (never a queue job id).
        requested = await client.post(
            f"/api/v1/cases/{case_id}/reports", json={"report_type": "full_disclosure"}
        )
        assert requested.status_code == 202
        report_id = requested.json()["data"]["report_id"]
        assert requested.json()["data"]["status"] == "queued"
        assert requested.headers["Location"] == f"/api/v1/reports/{report_id}"
        assert queue.enqueued == [("generate_case_report", (UUID(case_id), UUID(report_id)))]

        # 2. Poll via the advertised Location -> the row exists immediately, still queued.
        polled = await client.get(requested.headers["Location"])
        assert polled.status_code == 200
        assert polled.json()["data"]["report_id"] == report_id
        assert polled.json()["data"]["status"] == "queued"
        assert polled.json()["data"]["storage_ref"] is None
        assert polled.json()["data"]["generated_at"] is None

        # 3. Premature download is refused — there is no object to hand out yet.
        too_early = await client.get(f"/api/v1/reports/{report_id}/download")
        assert too_early.status_code == 409
        assert too_early.json()["error"]["code"] == "CONFLICT"

        # 4. The worker runs.
        await CaseService(uow, storage=storage).complete_report(
            UUID(report_id), storage, "corr-report-lifecycle"
        )

        # 5. Poll again -> the client can now see it finished.
        completed = await client.get(f"/api/v1/reports/{report_id}")
        assert completed.status_code == 200
        assert completed.json()["data"]["status"] == "completed"
        assert completed.json()["data"]["generated_at"] is not None

        # 6. Download -> a short-lived presigned URL, never the raw s3:// reference.
        download = await client.get(f"/api/v1/reports/{report_id}/download")
        assert download.status_code == 200
        url = download.json()["data"]["download_url"]
        assert url.startswith("http")
        assert not url.startswith("s3://")


async def test_requesting_a_report_for_an_unknown_case_is_404(uow) -> None:
    app = _app_with_overrides(uow, task_queue=_RecordingTaskQueue())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/cases/{uuid4()}/reports", json={"report_type": "summary"}
        )
    assert resp.status_code == 404


async def test_status_transition_via_api(uow) -> None:
    app = _app_with_overrides(uow)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = (await client.post("/api/v1/cases", json={"title": "X"})).json()["data"]
        resp = await client.post(
            f"/api/v1/cases/{created['case_id']}/status", json={"new_status": "closed"}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "closed"
