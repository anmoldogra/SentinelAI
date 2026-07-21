"""Domain exception hierarchy — guide Part 11.

Every class here maps 1:1 to an ``api-design.md`` §2.4 error code. Adding a domain
exception without a corresponding documented code (or vice versa) is a contract
violation, not a local choice. The HTTP mapping lives in the entrypoint's
``exception_handlers`` — these classes only carry ``code`` + ``http_status``.
"""

from __future__ import annotations

from typing import Any

ErrorDetails = list[dict[str, Any]]


class DomainError(Exception):
    """Base for every expected, mapped error. Unmapped errors become INTERNAL_ERROR."""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str | None = None,
        *,
        details: ErrorDetails | None = None,
    ) -> None:
        # Per-instance details (never a shared mutable class attribute).
        self.details: ErrorDetails = details if details is not None else []
        super().__init__(message or self.__class__.__name__)


class ValidationFailedError(DomainError):
    """A business/domain validation rule failed (distinct from malformed shape → 400)."""

    code, http_status = "VALIDATION_FAILED", 422

    def __init__(self, details: ErrorDetails) -> None:
        super().__init__("One or more fields failed validation", details=details)


class EvidenceImmutableError(DomainError):
    """Attempted mutation of an immutable evidence record (CEM / database-design.md §8)."""

    code, http_status = "EVIDENCE_IMMUTABLE", 409


class LegalHoldViolationError(DomainError):
    """A deletion/purge path was attempted against a legal-hold record (security §39)."""

    code, http_status = "LEGAL_HOLD_VIOLATION", 409


class NotFoundError(DomainError):
    """Requested resource does not exist or is not visible to the caller."""

    code, http_status = "NOT_FOUND", 404


class ForbiddenError(DomainError):
    """Authenticated but not authorized for this action/resource (RBAC/ABAC)."""

    code, http_status = "FORBIDDEN", 403


class UnauthenticatedError(DomainError):
    """No valid authentication was presented."""

    code, http_status = "UNAUTHENTICATED", 401


class ConflictError(DomainError):
    """A state-machine/precondition conflict (e.g. already-reviewed, version mismatch)."""

    code, http_status = "CONFLICT", 409


class PreconditionFailedError(DomainError):
    """An ``If-Match`` ETag did not match current state — optimistic-concurrency
    guard (api-design.md §2.6). Distinct from a 409 body conflict."""

    code, http_status = "PRECONDITION_FAILED", 412
