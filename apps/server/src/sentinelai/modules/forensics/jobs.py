"""forensics background jobs — arq (guide Part 12).

Artifact parsing/normalization runs as a background job; on completion it publishes
``forensics.artifact_processed``. Body deferred (``NotImplementedError``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def process_artifact(ctx: dict[str, Any], artifact_id: UUID) -> None:
    """Parse/normalize a registered artifact and update its row."""
    raise NotImplementedError
