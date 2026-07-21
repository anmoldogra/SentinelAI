"""osint domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class SourceNotFoundError(NotFoundError):
    """No OSINT source with the given id is registered."""


class FindingNotFoundError(NotFoundError):
    """No OSINT finding with the given id exists."""


class FindingAlreadyPublishedError(ConflictError):
    """The finding has already been published into ingestion.evidence."""
