"""forensics domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class ArtifactNotFoundError(NotFoundError):
    """No forensic artifact with the given id exists."""


class ArtifactAlreadyPublishedError(ConflictError):
    """The artifact has already been published into ingestion.evidence."""
