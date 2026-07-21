"""social_media event wiring — event-driven-architecture.md §25.6. Pure publisher."""

from __future__ import annotations

from sentinelai.platform.events.dispatcher import EventDispatcher

SCHEMA = "social_media"

# Published (§25.6).
EVENT_CONTENT_CAPTURED = "social_media.content_captured"
EVENT_ACCOUNT_REGISTERED = "social_media.account_registered"


def register_consumers(dispatcher: EventDispatcher) -> None:
    """Intentionally empty: social_media consumes no events (§25.6, connector-driven)."""
    return None
