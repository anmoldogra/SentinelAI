"""notification domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import NotFoundError


class NotificationNotFoundError(NotFoundError):
    """No notification with the given id exists for the caller."""


class NotificationRuleNotFoundError(NotFoundError):
    """No notification rule with the given id exists."""
