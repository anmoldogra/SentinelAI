"""threat_intel domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import NotFoundError


class IocNotFoundError(NotFoundError):
    """No IOC with the given id exists."""


class ThreatActorNotFoundError(NotFoundError):
    """No threat actor profile with the given id exists."""


class FeedSubscriptionNotFoundError(NotFoundError):
    """No feed subscription with the given id exists."""
