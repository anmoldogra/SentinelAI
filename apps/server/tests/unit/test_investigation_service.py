"""Unit tests for InvestigationService against an in-memory FakeInvestigationUnitOfWork."""

from __future__ import annotations

from uuid import uuid4

import pytest

from sentinelai.modules.investigation.exceptions import (
    FindingAlreadyReviewedError,
    RelationshipNotFoundError,
)
from sentinelai.modules.investigation.models import Relationship
from sentinelai.modules.investigation.schemas import EntityCreate
from sentinelai.modules.investigation.service import InvestigationService, relationship_etag
from sentinelai.shared.exceptions import PreconditionFailedError, ValidationFailedError
from sentinelai.shared.pagination import PageParams


def _proposed_relationship() -> Relationship:
    return Relationship(
        relationship_id=uuid4(),
        type="located_at",
        from_entity_id=uuid4(),
        to_entity_id=uuid4(),
        directional=True,
        confidence=0.7,
        status="proposed",
        created_by_type="ai",
        created_by_ref=uuid4(),
    )


async def test_create_entity_is_confirmed_by_analyst(inv_uow, actor) -> None:
    svc = InvestigationService(inv_uow)
    entity = await svc.create_entity(
        EntityCreate(entity_type="person", canonical_name="John Doe"), actor, "c"
    )
    assert entity.status == "confirmed"
    assert entity.created_by_type == "analyst"
    assert inv_uow.commits == 1


async def test_review_relationship_confirm(inv_uow, actor) -> None:
    rel = _proposed_relationship()
    await inv_uow.relationships.add(rel)
    svc = InvestigationService(inv_uow)
    updated = await svc.review_relationship_status(
        rel.relationship_id, "confirmed", "looks right", actor, "c", relationship_etag(rel)
    )
    assert updated.status == "confirmed"
    assert len(inv_uow.relationship_revisions.items) == 1
    assert any(
        e["event_type"] == "investigation.finding_reviewed" for e in inv_uow.outbox.published
    )


async def test_review_already_dispositioned_is_conflict(inv_uow, actor) -> None:
    rel = _proposed_relationship()
    rel.status = "confirmed"
    await inv_uow.relationships.add(rel)
    svc = InvestigationService(inv_uow)
    with pytest.raises(FindingAlreadyReviewedError):
        await svc.review_relationship_status(
            rel.relationship_id, "rejected", None, actor, "c", relationship_etag(rel)
        )


async def test_review_invalid_disposition(inv_uow, actor) -> None:
    rel = _proposed_relationship()
    await inv_uow.relationships.add(rel)
    svc = InvestigationService(inv_uow)
    with pytest.raises(ValidationFailedError):
        await svc.review_relationship_status(
            rel.relationship_id, "maybe", None, actor, "c", relationship_etag(rel)
        )


async def test_review_etag_mismatch(inv_uow, actor) -> None:
    rel = _proposed_relationship()
    await inv_uow.relationships.add(rel)
    svc = InvestigationService(inv_uow)
    with pytest.raises(PreconditionFailedError):
        await svc.review_relationship_status(
            rel.relationship_id, "confirmed", None, actor, "c", 'W/"stale"'
        )


async def test_review_missing_relationship(inv_uow, actor) -> None:
    svc = InvestigationService(inv_uow)
    with pytest.raises(RelationshipNotFoundError):
        await svc.review_relationship_status(uuid4(), "confirmed", None, actor, "c", 'W/"x"')


async def test_create_relationship_requires_supporting_evidence(inv_uow, actor) -> None:
    svc = InvestigationService(inv_uow)
    with pytest.raises(ValidationFailedError):
        await svc.create_relationship(
            case_id=uuid4(), rel_type="x", from_entity_id=uuid4(), to_entity_id=uuid4(),
            directional=True, confidence=0.5, evidence_ids=[], created_by_ref=uuid4(),
            correlation_id="c",
        )


async def test_create_relationship_publishes_correlation_generated(inv_uow, actor) -> None:
    svc = InvestigationService(inv_uow)
    rel = await svc.create_relationship(
        case_id=uuid4(), rel_type="located_at", from_entity_id=uuid4(), to_entity_id=uuid4(),
        directional=True, confidence=0.5, evidence_ids=[uuid4()], created_by_ref=uuid4(),
        correlation_id="c",
    )
    assert rel.status == "proposed"
    assert len(inv_uow.relationship_evidence.items) == 1
    assert any(
        e["event_type"] == "investigation.correlation_generated" for e in inv_uow.outbox.published
    )


async def test_review_queue_is_proposed_filter(inv_uow, actor) -> None:
    proposed = _proposed_relationship()
    reviewed = _proposed_relationship()
    reviewed.status = "confirmed"
    await inv_uow.relationships.add(proposed)
    await inv_uow.relationships.add(reviewed)
    svc = InvestigationService(inv_uow)
    items, _cursor, _more = await svc.list_relationships(
        actor, "proposed", PageParams(limit=50, cursor=None)
    )
    assert all(r.status == "proposed" for r in items)
    assert proposed in items and reviewed not in items
