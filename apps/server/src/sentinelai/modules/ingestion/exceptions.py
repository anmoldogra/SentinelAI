"""ingestion domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class EvidenceNotFoundError(NotFoundError):
    """No evidence with the given id is visible to the caller."""


class ConnectorNotFoundError(NotFoundError):
    """No connector with the given id is registered."""


class EvidenceAlreadySupersededError(ConflictError):
    """The evidence has already been superseded by a later version."""
