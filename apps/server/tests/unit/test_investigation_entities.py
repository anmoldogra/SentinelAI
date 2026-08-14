"""Unit tests for entity review + list pagination (coverage the review suite lacked)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from sentinelai.modules.investigation.exceptions import (
    EntityNotFoundError,
    FindingAlreadyReviewedError,
)
from sentinelai.modules.investigation.models import Entity
from sentinelai.modules.investigation.service import InvestigationService, entity_etag
from sentinelai.shared.exceptions import PreconditionFailedError, ValidationFailedError
from sentinelai.shared.pagination import PageParams


def _proposed_entity() -> Entity:
    return Entity(
        entity_id=uuid4(),
        entity_type="person",
        canonical_name="Jane Roe",
        aliases=None,
        status="proposed",
        confidence=Decimal("0.7"),
        created_by_type="ai",
        created_by_ref=uuid4(),
    )


async def test_review_entity_confirm_writes_revision_and_no_event(inv_uow, actor) -> None:
    entity = _proposed_entity()
    await inv_uow.entities.add(entity)
    svc = InvestigationService(inv_uow)
    updated = await svc.review_entity_status(
        entity.entity_id, "confirmed", actor, "c", entity_etag(entity)
    )
    assert updated.status == "confirmed"
    assert len(inv_uow.entity_revisions.items) == 1
    # Entity review has no documented event (§25.8's finding_reviewed is relationship-only).
    assert inv_uow.outbox.published == []


async def test_review_entity_already_reviewed_is_conflict(inv_uow, actor) -> None:
    entity = _proposed_entity()
    entity.status = "rejected"
    await inv_uow.entities.add(entity)
    svc = InvestigationService(inv_uow)
    with pytest.raises(FindingAlreadyReviewedError):
        await svc.review_entity_status(
            entity.entity_id, "confirmed", actor, "c", entity_etag(entity)
        )


async def test_review_entity_invalid_disposition(inv_uow, actor) -> None:
    entity = _proposed_entity()
    await inv_uow.entities.add(entity)
    svc = InvestigationService(inv_uow)
    with pytest.raises(ValidationFailedError):
        await svc.review_entity_status(
            entity.entity_id, "archived", actor, "c", entity_etag(entity)
        )


async def test_review_entity_etag_mismatch(inv_uow, actor) -> None:
    entity = _proposed_entity()
    await inv_uow.entities.add(entity)
    svc = InvestigationService(inv_uow)
    with pytest.raises(PreconditionFailedError):
        await svc.review_entity_status(entity.entity_id, "confirmed", actor, "c", 'W/"stale"')


async def test_get_entity_not_found(inv_uow, actor) -> None:
    svc = InvestigationService(inv_uow)
    with pytest.raises(EntityNotFoundError):
        await svc.get_entity(uuid4(), actor)


async def test_list_entities_pagination_reports_has_more(inv_uow, actor) -> None:
    for _ in range(3):
        await inv_uow.entities.add(_proposed_entity())
    svc = InvestigationService(inv_uow)
    items, next_cursor, has_more = await svc.list_entities(
        actor, None, PageParams(limit=2, cursor=None)
    )
    assert len(items) == 2
    assert has_more is True
    assert next_cursor is not None
