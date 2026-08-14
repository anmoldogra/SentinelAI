"""ingestion background jobs — arq (guide Part 9 & 12).

Malware scanning runs as a background job after upload confirmation; promotion from
the ``quarantine`` bucket to the ``evidence`` bucket happens only after a clean scan
(security §24-25's category-aware policy is enforced in the service, not here).
Bodies deferred (``NotImplementedError``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def scan_uploaded_evidence(ctx: dict[str, Any], evidence_id: UUID) -> None:
    """Scan a freshly-uploaded object; promote it out of quarantine on a clean result."""
    raise NotImplementedError
