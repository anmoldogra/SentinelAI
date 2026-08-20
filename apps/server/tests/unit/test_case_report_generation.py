"""Unit tests for async case-report generation (api-design.md §7, guide Part 12).

Covers the two halves of the job-state-row pattern: the request path creating a ``queued`` row and
enqueueing, and the worker path rendering, storing, completing, and announcing it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from sentinelai.modules.case_management.exceptions import (
    ReportNotFoundError,
    ReportNotReadyError,
)
from sentinelai.modules.case_management.models import (
    REPORT_COMPLETED,
    REPORT_FAILED,
    REPORT_QUEUED,
)
from sentinelai.modules.case_management.schemas import CaseCreate, CaseReportCreate
from sentinelai.modules.case_management.service import CaseService
from sentinelai.platform.config import settings
from sentinelai.platform.storage import parse_object_uri
from tests.fixtures.fake_object_storage import FakeObjectStorage


class _FakeTaskQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, name: str, *args: Any) -> Any:
        self.enqueued.append((name, args))
        return None


async def _case_with_report(uow: Any, actor: Any) -> tuple[CaseService, Any, Any, _FakeTaskQueue]:
    service = CaseService(uow, storage=FakeObjectStorage())
    case = await service.create_case(CaseCreate(title="Operation X"), actor, "corr-1")
    queue = _FakeTaskQueue()
    report = await service.generate_report(
        case.case_id, CaseReportCreate(report_type="full_disclosure"), actor, "corr-1", queue
    )
    return service, case, report, queue


# --- the request path: a queued job-state row --------------------------------


async def test_requesting_a_report_creates_a_queued_row(uow, actor) -> None:
    _, case, report, _ = await _case_with_report(uow, actor)
    assert report.status == REPORT_QUEUED
    assert report.case_id == case.case_id
    assert report.generated_by_user_id == actor.user_id
    assert report.requested_at is not None


async def test_a_queued_row_has_no_object_or_completion_time_yet(uow, actor) -> None:
    """The schema conflict this increment resolved: both are NULL until the job runs."""
    _, _, report, _ = await _case_with_report(uow, actor)
    assert report.storage_ref is None
    assert report.generated_at is None


async def test_the_job_is_enqueued_with_both_the_case_and_the_report(uow, actor) -> None:
    """The job needs the report_id — the row is its state, not the queue."""
    _, case, report, queue = await _case_with_report(uow, actor)
    assert queue.enqueued == [("generate_case_report", (case.case_id, report.report_id))]


async def test_the_queued_row_is_immediately_pollable(uow, actor) -> None:
    service, _, report, _ = await _case_with_report(uow, actor)
    polled = await service.get_report(report.report_id, actor)
    assert polled.report_id == report.report_id
    assert polled.status == REPORT_QUEUED


async def test_requesting_a_report_never_commits(uow, actor) -> None:
    """ADR-0005: the router owns the transaction."""
    before = uow.commits
    await _case_with_report(uow, actor)
    assert uow.commits == before


# --- the worker path: render, store, complete, announce ----------------------


async def test_completing_a_report_stores_a_json_object(uow, actor) -> None:
    service, case, report, _ = await _case_with_report(uow, actor)
    storage = FakeObjectStorage()

    await service.complete_report(report.report_id, storage, "corr-1")

    bucket, key = parse_object_uri(report.storage_ref or "")
    assert bucket == settings.storage_bucket
    assert key == f"reports/{case.case_id}/{report.report_id}.json"
    assert await storage.exists(bucket, key) is True


async def test_the_stored_document_serialises_the_case(uow, actor) -> None:
    service, case, report, _ = await _case_with_report(uow, actor)
    storage = FakeObjectStorage()
    await service.complete_report(report.report_id, storage, "corr-1")

    bucket, key = parse_object_uri(report.storage_ref or "")
    raw = b"".join([chunk async for chunk in storage.get_stream(bucket, key)])
    document = json.loads(raw)
    assert document["case"]["case_id"] == str(case.case_id)
    assert document["case"]["title"] == "Operation X"
    assert document["report"]["report_id"] == str(report.report_id)
    assert "evidence" in document and "status_history" in document


async def test_completing_a_report_marks_it_completed(uow, actor) -> None:
    service, _, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")
    assert report.status == REPORT_COMPLETED
    assert report.generated_at is not None
    assert report.failure_reason is None


async def test_completing_a_report_publishes_the_generated_event(uow, actor) -> None:
    service, case, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")

    published = [e for e in uow.outbox.published if e["event_type"] == "case.report_generated"]
    assert len(published) == 1
    payload = published[0]["payload"]
    assert payload["report_id"] == str(report.report_id)
    assert payload["case_id"] == str(case.case_id)
    # The recipient the notification consumer needs (§25.7).
    assert payload["requested_by_user_id"] == str(actor.user_id)


async def test_completing_a_report_never_commits(uow, actor) -> None:
    service, _, report, _ = await _case_with_report(uow, actor)
    before = uow.commits
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")
    assert uow.commits == before


async def test_rerunning_a_completed_job_is_a_no_op(uow, actor) -> None:
    """arq retries: a redelivery must not re-upload or re-announce."""
    service, _, report, _ = await _case_with_report(uow, actor)
    storage = FakeObjectStorage()
    await service.complete_report(report.report_id, storage, "corr-1")
    first_ref, first_time = report.storage_ref, report.generated_at

    await service.complete_report(report.report_id, storage, "corr-1")

    assert (report.storage_ref, report.generated_at) == (first_ref, first_time)
    assert len([e for e in uow.outbox.published if e["event_type"] == "case.report_generated"]) == 1


async def test_completing_an_unknown_report_raises_not_found(uow, actor) -> None:
    service = CaseService(uow, storage=FakeObjectStorage())
    with pytest.raises(ReportNotFoundError):
        await service.complete_report(uuid4(), FakeObjectStorage(), "corr-1")


async def test_a_storage_failure_leaves_the_report_incomplete(uow, actor) -> None:
    """The row must never advertise a report that was not stored."""

    class _BrokenStorage(FakeObjectStorage):
        async def put_stream(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("object store down")

    service, _, report, _ = await _case_with_report(uow, actor)
    with pytest.raises(RuntimeError, match="object store down"):
        await service.complete_report(report.report_id, _BrokenStorage(), "corr-1")

    assert report.status != REPORT_COMPLETED
    assert report.storage_ref is None
    assert uow.outbox.published[-1]["event_type"] != "case.report_generated"


# --- failure recording + download gating -------------------------------------


async def test_failing_a_report_records_the_reason_for_a_poller(uow, actor) -> None:
    service, _, report, _ = await _case_with_report(uow, actor)
    await service.fail_report(report.report_id, "RuntimeError")
    assert report.status == REPORT_FAILED
    assert report.failure_reason == "RuntimeError"


async def test_a_completed_report_is_never_downgraded_to_failed(uow, actor) -> None:
    service, _, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")
    await service.fail_report(report.report_id, "LateError")
    assert report.status == REPORT_COMPLETED


async def test_the_download_url_targets_the_stored_object(uow, actor) -> None:
    service, _, report, _ = await _case_with_report(uow, actor)
    storage = FakeObjectStorage()
    await service.complete_report(report.report_id, storage, "corr-1")

    url = await service.get_report_download_url(report.report_id, actor)
    bucket, key = parse_object_uri(report.storage_ref or "")
    assert f"{bucket}/{key}" in url
    assert "method=GET" in url


async def test_the_download_url_is_short_lived(uow, actor) -> None:
    """A disclosure credential must expire (ADR-0008 §6)."""
    service, _, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")
    url = await service.get_report_download_url(report.report_id, actor)
    assert "expires_in=900" in url


async def test_minting_a_download_url_records_the_disclosure_in_the_audit_log(
    uow, actor, monkeypatch
) -> None:
    """api-design.md §7: a report leaving the system is disclosure-significant."""
    recorded: list[dict[str, Any]] = []

    async def _capture(session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr("sentinelai.modules.case_management.service.record_audit_event", _capture)
    service, case, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")
    recorded.clear()

    await service.get_report_download_url(report.report_id, actor)

    entry = next(e for e in recorded if e["action"] == "case.report_downloaded")
    assert entry["actor_user_id"] == actor.user_id
    assert entry["target_id"] == report.report_id
    assert entry["module"] == "case_management"
    assert entry["details"]["case_id"] == str(case.case_id)


async def test_the_audit_entry_never_carries_the_url_itself(uow, actor, monkeypatch) -> None:
    """The presigned URL is a bearer credential — auditing it would persist a live secret."""
    recorded: list[dict[str, Any]] = []

    async def _capture(session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr("sentinelai.modules.case_management.service.record_audit_event", _capture)
    service, _, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")
    recorded.clear()

    url = await service.get_report_download_url(report.report_id, actor)
    assert all(url not in str(entry) for entry in recorded)


async def test_a_failed_audit_write_yields_no_url(uow, actor, monkeypatch) -> None:
    """No un-audited disclosure credential may escape: the caller gets the error, not the URL."""

    async def _explode(session: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit log unavailable")

    # Patch only AFTER setup — creating the case and report audit too.
    service, _, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")
    monkeypatch.setattr("sentinelai.modules.case_management.service.record_audit_event", _explode)

    with pytest.raises(RuntimeError, match="audit log unavailable"):
        await service.get_report_download_url(report.report_id, actor)


async def test_minting_a_download_url_never_commits(uow, actor) -> None:
    """ADR-0005: the router commits the audit write, not the service."""
    service, _, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")
    before = uow.commits
    await service.get_report_download_url(report.report_id, actor)
    assert uow.commits == before


async def test_an_unfinished_report_is_never_audited_as_disclosed(uow, actor, monkeypatch) -> None:
    recorded: list[dict[str, Any]] = []

    async def _capture(session: Any, **kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr("sentinelai.modules.case_management.service.record_audit_event", _capture)
    service, _, report, _ = await _case_with_report(uow, actor)
    recorded.clear()

    with pytest.raises(ReportNotReadyError):
        await service.get_report_download_url(report.report_id, actor)
    assert recorded == []


async def test_downloading_an_unfinished_report_is_refused(uow, actor) -> None:
    service, _, report, _ = await _case_with_report(uow, actor)
    with pytest.raises(ReportNotReadyError):
        await service.get_report_download_url(report.report_id, actor)


async def test_downloading_a_completed_report_returns_a_presigned_url(uow, actor) -> None:
    """api-design.md §7: a short-lived presigned URL, never the raw s3:// reference."""
    service, _, report, _ = await _case_with_report(uow, actor)
    await service.complete_report(report.report_id, FakeObjectStorage(), "corr-1")

    url = await service.get_report_download_url(report.report_id, actor)
    assert url.startswith("http")
    assert not url.startswith("s3://")
