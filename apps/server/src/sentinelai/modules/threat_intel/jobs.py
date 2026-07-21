"""threat_intel background jobs — arq (guide Part 12).

On-demand and scheduled feed synchronization (STIX/TAXII or vendor API). The
subscription row is the job's state. Body deferred (``NotImplementedError``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def sync_feed_subscription(ctx: dict[str, Any], subscription_id: UUID) -> None:
    """Pull the feed, upsert IOCs, and stamp ``last_synced_at``."""
    raise NotImplementedError
