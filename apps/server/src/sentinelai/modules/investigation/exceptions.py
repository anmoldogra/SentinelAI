"""investigation domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class EntityNotFoundError(NotFoundError):
    """No entity with the given id exists."""


class RelationshipNotFoundError(NotFoundError):
    """No relationship with the given id exists."""


class CorrelationRunNotFoundError(NotFoundError):
    """No correlation run with the given id exists."""


class FindingAlreadyReviewedError(ConflictError):
    """The entity/relationship has already been dispositioned (not still ``proposed``)."""
