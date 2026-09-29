"""notification domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class NotificationNotFoundError(NotFoundError):
    """No notification with the given id exists for the caller."""


class NotificationRuleNotFoundError(NotFoundError):
    """No notification rule with the given id exists."""


class NothingToRedeliverError(ConflictError):
    """The notification has no failed delivery attempt to retry — api-design.md §4.9's 409.

    §4.9 defines the endpoint as "Retry a **failed** delivery", so a notification whose latest
    attempt succeeded has nothing to retry. Refusing is the safer answer than sending again: §25.9
    works hard to guarantee an analyst never receives one fact twice, and an admin clicking retry on
    a delivered message would undo that guarantee by hand.
    """
