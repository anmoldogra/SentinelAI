"""Event-consumer registration — the one list both entrypoints agree on (ADR-0006).

The registrar list lived in ``entrypoints/http/main.py`` while the dispatcher ran there. Wave 2.2
moved the dispatcher to the worker, and copying the list would have been the obvious mistake: two
lists drift, and the failure mode is a handler that silently never runs in production because only
the process that no longer dispatches knew about it.

``entrypoints`` is the top of the import DAG, so this is the correct place for it — it is the only
layer allowed to know about every module at once.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sentinelai.modules.case_management import events as cm_events
from sentinelai.modules.forensics import events as forensics_events
from sentinelai.modules.ingestion import events as ingestion_events
from sentinelai.modules.investigation import events as investigation_events
from sentinelai.modules.notification import events as notification_events
from sentinelai.modules.osint import events as osint_events
from sentinelai.modules.social_media import events as social_media_events
from sentinelai.modules.threat_intel import events as threat_intel_events
from sentinelai.platform.events.dispatcher import EventDispatcher


class ConsumerRegistrar(Protocol):
    """What every module's ``events.register_consumers`` looks like."""

    def __call__(self, dispatcher: EventDispatcher) -> None: ...


CONSUMER_REGISTRARS: Sequence[ConsumerRegistrar] = (
    ingestion_events.register_consumers,
    osint_events.register_consumers,
    threat_intel_events.register_consumers,
    forensics_events.register_consumers,
    social_media_events.register_consumers,
    cm_events.register_consumers,
    investigation_events.register_consumers,
    notification_events.register_consumers,
)


def register_all(dispatcher: EventDispatcher) -> EventDispatcher:
    """Attach every module's consumers to ``dispatcher`` and return it."""
    for register in CONSUMER_REGISTRARS:
        register(dispatcher)
    return dispatcher


__all__ = ["CONSUMER_REGISTRARS", "ConsumerRegistrar", "register_all"]
