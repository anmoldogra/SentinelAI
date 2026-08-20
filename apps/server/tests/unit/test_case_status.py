"""Unit tests for the case status machine + ETag helpers (pure logic, no DB)."""

from __future__ import annotations

from types import SimpleNamespace

from sentinelai.modules.case_management.models import (
    STATUS_ARCHIVED,
    STATUS_CLOSED,
    STATUS_OPEN,
    VALID_STATUSES,
)
from sentinelai.modules.case_management.models import (
    TRANSITIONS as _TRANSITIONS,
)
from sentinelai.modules.case_management.service import _normalize_etag, case_etag


def test_documented_states() -> None:
    assert {STATUS_OPEN, STATUS_CLOSED, STATUS_ARCHIVED} == VALID_STATUSES


def test_transition_edges() -> None:
    assert _TRANSITIONS[STATUS_OPEN] == {STATUS_CLOSED, STATUS_ARCHIVED}
    assert _TRANSITIONS[STATUS_CLOSED] == {STATUS_OPEN, STATUS_ARCHIVED}
    assert _TRANSITIONS[STATUS_ARCHIVED] == set()  # terminal


def test_etag_depends_on_content() -> None:
    a = SimpleNamespace(title="Op A", description=None, status="open")
    a2 = SimpleNamespace(title="Op A", description=None, status="open")
    b = SimpleNamespace(title="Op B", description=None, status="open")
    assert case_etag(a) == case_etag(a2)
    assert case_etag(a) != case_etag(b)
    assert case_etag(a).startswith('W/"')


def test_normalize_etag_strips_weak_and_quotes() -> None:
    assert _normalize_etag('W/"abc"') == "abc"
    assert _normalize_etag('"abc"') == "abc"
    assert _normalize_etag("  abc  ") == "abc"
