"""osint event wiring — event-driven-architecture.md §25.3. Pure publisher (no consumers)."""

from __future__ import annotations

from sentinelai.platform.events.dispatcher import EventDispatcher

SCHEMA = "osint"

# Published (§25.3).
EVENT_FINDING_CAPTURED = "osint.finding_captured"
EVENT_SOURCE_ACTIVATED = "osint.source_activated"
EVENT_SOURCE_DEACTIVATED = "osint.source_deactivated"


def register_consumers(dispatcher: EventDispatcher) -> None:
    """Intentionally empty: osint consumes no events (§25.3, connector-driven)."""
    return None
