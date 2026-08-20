"""Unit tests for the Case aggregate's own state machine (ADR-0011 §1).

Pure aggregate behaviour, no service and no DB: the point of the rich-aggregate rule is that an
illegal state is unrepresentable through the aggregate's surface — these tests prove that holds
without any orchestration around it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from sentinelai.modules.case_management.exceptions import InvalidCaseStatusTransitionError
from sentinelai.modules.case_management.models import (
    STATUS_ARCHIVED,
    STATUS_CLOSED,
    STATUS_OPEN,
    Case,
)
from sentinelai.shared.exceptions import ValidationFailedError

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _case(status: str = STATUS_OPEN) -> Case:
    return Case(
        case_id=uuid4(),
        title="A case",
        description=None,
        status=status,
        owning_user_id=uuid4(),
        created_at=_NOW,
        closed_at=_NOW if status == STATUS_CLOSED else None,
    )


# --- legal transitions ------------------------------------------------------


def test_close_moves_open_to_closed_and_stamps_closed_at() -> None:
    case = _case(STATUS_OPEN)
    previous = case.close(at=_NOW)
    assert (previous, case.status, case.closed_at) == (STATUS_OPEN, STATUS_CLOSED, _NOW)


def test_reopen_moves_closed_to_open_and_clears_closed_at() -> None:
    case = _case(STATUS_CLOSED)
    previous = case.reopen(at=_NOW)
    assert (previous, case.status, case.closed_at) == (STATUS_CLOSED, STATUS_OPEN, None)


@pytest.mark.parametrize("start", [STATUS_OPEN, STATUS_CLOSED])
def test_archive_is_reachable_from_open_and_closed(start: str) -> None:
    case = _case(start)
    assert case.archive(at=_NOW) == start
    assert case.status == STATUS_ARCHIVED


def test_transition_to_returns_the_previous_status() -> None:
    case = _case(STATUS_OPEN)
    assert case.transition_to(STATUS_CLOSED, at=_NOW) == STATUS_OPEN


# --- refused transitions ----------------------------------------------------


def test_closing_an_already_closed_case_is_refused() -> None:
    case = _case(STATUS_CLOSED)
    with pytest.raises(InvalidCaseStatusTransitionError):
        case.close(at=_NOW)
    assert case.status == STATUS_CLOSED  # unchanged — the aggregate refused, not half-applied


@pytest.mark.parametrize("target", [STATUS_OPEN, STATUS_CLOSED])
def test_archived_is_terminal(target: str) -> None:
    case = _case(STATUS_OPEN)
    case.archive(at=_NOW)
    with pytest.raises(InvalidCaseStatusTransitionError):
        case.transition_to(target, at=_NOW)
    assert case.status == STATUS_ARCHIVED


def test_reopening_an_open_case_is_refused() -> None:
    with pytest.raises(InvalidCaseStatusTransitionError):
        _case(STATUS_OPEN).reopen(at=_NOW)


def test_a_status_outside_the_vocabulary_is_rejected_as_validation() -> None:
    with pytest.raises(ValidationFailedError):
        _case(STATUS_OPEN).transition_to("suspended", at=_NOW)


def test_a_refused_transition_never_touches_closed_at() -> None:
    case = _case(STATUS_CLOSED)
    stamped = case.closed_at
    with pytest.raises(InvalidCaseStatusTransitionError):
        case.close(at=datetime(2027, 1, 1, tzinfo=UTC))
    assert case.closed_at == stamped
