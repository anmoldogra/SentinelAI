"""Canonical domain-exception hierarchy tests (guide Part 11).

Replaces the reverted duplicate-taxonomy test. Every class in ``sentinelai.shared.exceptions`` must
carry a ``code`` + ``http_status`` that matches api-design.md §2.4 exactly — adding one without a
documented code is a contract violation — so this guards the mapping and the per-instance details.
"""

from __future__ import annotations

import pytest

from sentinelai.shared.exceptions import (
    ConflictError,
    DomainError,
    EvidenceImmutableError,
    ForbiddenError,
    LegalHoldViolationError,
    NotFoundError,
    PreconditionFailedError,
    UnauthenticatedError,
    ValidationFailedError,
)

# api-design.md §2.4 — the stable code -> HTTP status contract.
_FROZEN: dict[str, int] = {
    "VALIDATION_FAILED": 422,  # 422 = domain-rule failure (malformed shape is 400, handled apart)
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "EVIDENCE_IMMUTABLE": 409,
    "LEGAL_HOLD_VIOLATION": 409,
    "PRECONDITION_FAILED": 412,
    "INTERNAL_ERROR": 500,
}

_CASES: list[tuple[type[DomainError], str, int]] = [
    (DomainError, "INTERNAL_ERROR", 500),
    (EvidenceImmutableError, "EVIDENCE_IMMUTABLE", 409),
    (LegalHoldViolationError, "LEGAL_HOLD_VIOLATION", 409),
    (NotFoundError, "NOT_FOUND", 404),
    (ForbiddenError, "FORBIDDEN", 403),
    (UnauthenticatedError, "UNAUTHENTICATED", 401),
    (ConflictError, "CONFLICT", 409),
    (PreconditionFailedError, "PRECONDITION_FAILED", 412),
]


@pytest.mark.parametrize(("exc", "code", "status"), _CASES)
def test_code_and_http_status_match_the_frozen_contract(
    exc: type[DomainError], code: str, status: int
) -> None:
    instance = exc()
    assert instance.code == code
    assert instance.http_status == status
    assert _FROZEN[instance.code] == instance.http_status  # never a code outside api-design §2.4


def test_validation_failed_carries_details_and_maps_to_422() -> None:
    details = [{"field": "attributes.sender", "issue": "required"}]
    err = ValidationFailedError(details)
    assert err.code == "VALIDATION_FAILED"
    assert err.http_status == 422
    assert err.details == details


def test_details_default_is_per_instance_not_shared() -> None:
    first, second = NotFoundError(), NotFoundError()
    first.details.append({"field": "x", "issue": "y"})
    assert second.details == []  # a shared mutable class attribute would leak between instances


def test_message_defaults_to_class_name() -> None:
    assert str(NotFoundError()) == "NotFoundError"
    assert str(ConflictError("already reviewed")) == "already reviewed"
