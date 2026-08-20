"""Unit tests for CaseService business logic against an in-memory FakeUnitOfWork."""

from __future__ import annotations

from uuid import uuid4

import pytest

from sentinelai.modules.case_management.exceptions import (
    CaseNotFoundError,
    EvidenceAlreadyLinkedError,
    EvidenceLinkNotFoundError,
    InvalidCaseStatusTransitionError,
)
from sentinelai.modules.case_management.schemas import (
    CaseCreate,
    CaseStatusUpdate,
    CaseUpdate,
    EvidenceLinkCreate,
)
from sentinelai.modules.case_management.service import CaseService, case_etag
from sentinelai.platform.auth.dependencies import CurrentUser
from sentinelai.shared.exceptions import ForbiddenError, PreconditionFailedError
from tests.fixtures.fake_object_storage import FakeObjectStorage


async def test_create_case_opens_and_publishes_without_committing(uow, actor) -> None:
    svc = CaseService(uow, storage=FakeObjectStorage())
    case = await svc.create_case(CaseCreate(title="Op X"), actor, "corr-1")
    assert case.status == "open"
    assert case.owning_user_id == actor.user_id
    assert uow.commits == 0  # ADR-0005: the entrypoint, not the service, commits
    assert [e["event_type"] for e in uow.outbox.published] == ["case.created"]


async def test_change_status_open_to_closed(uow, actor) -> None:
    svc = CaseService(uow, storage=FakeObjectStorage())
    case = await svc.create_case(CaseCreate(title="X"), actor, "c")
    updated = await svc.change_status(
        case.case_id, CaseStatusUpdate(new_status="closed"), actor, "c"
    )
    assert updated.status == "closed"
    assert updated.closed_at is not None
    assert any(e["event_type"] == "case.status_changed" for e in uow.outbox.published)
    assert len(uow.status_history.items) == 1


async def test_archived_is_terminal(uow, actor) -> None:
    svc = CaseService(uow, storage=FakeObjectStorage())
    case = await svc.create_case(CaseCreate(title="X"), actor, "c")
    await svc.change_status(case.case_id, CaseStatusUpdate(new_status="archived"), actor, "c")
    with pytest.raises(InvalidCaseStatusTransitionError):
        await svc.change_status(case.case_id, CaseStatusUpdate(new_status="open"), actor, "c")


async def test_update_requires_matching_etag(uow, actor) -> None:
    svc = CaseService(uow, storage=FakeObjectStorage())
    case = await svc.create_case(CaseCreate(title="X"), actor, "c")
    with pytest.raises(PreconditionFailedError):
        await svc.update_case(case.case_id, CaseUpdate(title="Y"), actor, 'W/"stale"')
    updated = await svc.update_case(case.case_id, CaseUpdate(title="Y"), actor, case_etag(case))
    assert updated.title == "Y"


async def test_link_evidence_dedup(uow, actor) -> None:
    svc = CaseService(uow, storage=FakeObjectStorage())
    case = await svc.create_case(CaseCreate(title="X"), actor, "c")
    evidence_id = uuid4()
    await svc.link_evidence(case.case_id, EvidenceLinkCreate(evidence_id=evidence_id), actor, "c")
    with pytest.raises(EvidenceAlreadyLinkedError):
        await svc.link_evidence(
            case.case_id, EvidenceLinkCreate(evidence_id=evidence_id), actor, "c"
        )
    assert any(e["event_type"] == "evidence.linked_to_case" for e in uow.outbox.published)


async def test_unlink_missing_link_raises(uow, actor) -> None:
    svc = CaseService(uow, storage=FakeObjectStorage())
    case = await svc.create_case(CaseCreate(title="X"), actor, "c")
    with pytest.raises(EvidenceLinkNotFoundError):
        await svc.unlink_evidence(case.case_id, uuid4(), actor, "c")


async def test_ownership_enforced_on_read(uow, actor) -> None:
    svc = CaseService(uow, storage=FakeObjectStorage())
    case = await svc.create_case(CaseCreate(title="X"), actor, "c")
    stranger = CurrentUser(user_id=uuid4(), roles=("investigator",))
    with pytest.raises(ForbiddenError):
        await svc.get_case(case.case_id, stranger)


async def test_get_missing_case_raises(uow, actor) -> None:
    svc = CaseService(uow, storage=FakeObjectStorage())
    with pytest.raises(CaseNotFoundError):
        await svc.get_case(uuid4(), actor)
