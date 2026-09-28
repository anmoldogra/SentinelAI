"""Unit tests for investigation review dispositions + ETag helpers (pure logic)."""

from __future__ import annotations

from types import SimpleNamespace

from sentinelai.modules.investigation.models import REVIEW_DISPOSITIONS
from sentinelai.modules.investigation.service import (
    STATUS_CONFIRMED,
    STATUS_REJECTED,
    _normalize_etag,
    entity_etag,
    relationship_etag,
)


def test_review_dispositions() -> None:
    """The vocabulary moved to the aggregate in Wave 2.4 (ADR-0011 §1).

    Asserted against the service's re-exported names too, so the two cannot drift: the router and
    schemas still import them from the service.
    """
    assert {STATUS_CONFIRMED, STATUS_REJECTED} == REVIEW_DISPOSITIONS


def test_entity_etag_depends_on_content() -> None:
    a = SimpleNamespace(canonical_name="A", aliases=None, status="proposed", confidence=0.7)
    b = SimpleNamespace(canonical_name="B", aliases=None, status="proposed", confidence=0.7)
    assert entity_etag(a) != entity_etag(b)
    assert entity_etag(a).startswith('W/"')


def test_relationship_etag_depends_on_status() -> None:
    a = SimpleNamespace(type="located_at", status="proposed", confidence=0.7)
    b = SimpleNamespace(type="located_at", status="confirmed", confidence=0.7)
    assert relationship_etag(a) != relationship_etag(b)


def test_normalize_etag() -> None:
    assert _normalize_etag('W/"abc"') == "abc"
    assert _normalize_etag('"abc"') == "abc"
