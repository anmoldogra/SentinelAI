"""threat_intel background jobs — arq (guide Part 12).

**The feed transport is not implemented, and this module says so rather than pretending.**
`api-design.md` §4.4 documents `POST /feeds/{subscription_id}/sync` as async, and
`ThreatIntelService.sync_feed` does everything that needs no transport: it validates the
subscription, refuses outright on a zero-egress profile, audits the request, and enqueues this job.
What is missing is the STIX/TAXII and vendor-API client that would actually pull indicators — a
connector increment of its own, needing protocol parsing, credential handling and a poll schedule.

Raising here is the honest behaviour for that state. The endpoint is `202 Accepted`, so a failing
job dead-letters where an operator sees it; it does not tell a caller the sync succeeded. The
alternative — stamping `last_synced_at` and returning — would record a synchronization that never
happened, on the column an analyst reads to decide whether their threat library is current. A feed
that silently never updates while claiming it did is worse than one that visibly fails.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sentinelai.platform.config import settings
from sentinelai.platform.logging import log


class FeedTransportNotConfigured(RuntimeError):
    """No feed transport is available to satisfy a sync request.

    Named rather than a bare ``NotImplementedError`` so the dead-letter reason an operator reads
    names the actual cause, and so the air-gapped refusal is distinguishable from the not-built one.
    They need different responses: one is policy working, the other is a missing feature.
    """


async def sync_feed_subscription(ctx: dict[str, Any], subscription_id: UUID) -> None:
    """Pull the feed, upsert IOCs, and stamp ``last_synced_at``.

    Re-checks the egress policy even though `sync_feed` already refused at the API boundary: a job
    can be enqueued on one profile and run after a redeploy onto another, and arq retries outlive
    the request that queued them. The worker verifies the invariant rather than trusting whoever
    enqueued this.
    """
    if settings.is_air_gapped:
        log.error(
            "feed_sync_refused",
            reason="zero-egress profile",
            profile=settings.app_env,
            subscription_id=str(subscription_id),
        )
        raise FeedTransportNotConfigured(
            f"feed synchronization requires egress, which the '{settings.app_env}' profile forbids"
        )

    log.error(
        "feed_sync_unavailable",
        reason="no STIX/TAXII or vendor-API transport is built",
        subscription_id=str(subscription_id),
    )
    raise FeedTransportNotConfigured(
        "no feed transport is configured: STIX/TAXII and vendor-API clients are not built. "
        "IOCs can be registered through POST /api/v1/threat-intel/iocs in the meantime."
    )


__all__ = ["FeedTransportNotConfigured", "sync_feed_subscription"]
