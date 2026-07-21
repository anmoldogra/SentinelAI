"""social_media domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class AccountNotFoundError(NotFoundError):
    """No monitored account with the given id exists."""


class ContentNotFoundError(NotFoundError):
    """No captured content with the given id exists."""


class ContentAlreadyPublishedError(ConflictError):
    """The content has already been published into ingestion.evidence."""
