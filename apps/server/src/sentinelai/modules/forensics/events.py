"""forensics event wiring — event-driven-architecture.md §25.5. Pure publisher."""

from __future__ import annotations

from sentinelai.platform.events.dispatcher import EventDispatcher

SCHEMA = "forensics"

# Published (§25.5).
EVENT_ARTIFACT_REGISTERED = "forensics.artifact_registered"
EVENT_ARTIFACT_PROCESSED = "forensics.artifact_processed"


def register_consumers(dispatcher: EventDispatcher) -> None:
    """Intentionally empty: forensics consumes no events (§25.5, examiner-driven)."""
    return None
