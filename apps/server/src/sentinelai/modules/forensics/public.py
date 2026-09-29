"""forensics public interface — the ONLY symbols other modules may import.

Nothing consumes this today: §25.5 makes `forensics` a pure publisher, and a downstream module
learns about a forensic artifact through the canonical `evidence.ingested` event that publication
produces, not by asking this module. The interface exists so that when something does need to ask,
the answer is a schema and a service rather than a table.
"""

from __future__ import annotations

from sentinelai.modules.forensics.schemas import ArtifactRead
from sentinelai.modules.forensics.service import ForensicsService

__all__ = ["ArtifactRead", "ForensicsService"]
