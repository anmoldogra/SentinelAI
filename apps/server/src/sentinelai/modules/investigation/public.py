"""investigation public interface — the ONLY symbols other modules may import."""

from __future__ import annotations

from sentinelai.modules.investigation.schemas import EntityRead, RelationshipRead
from sentinelai.modules.investigation.service import InvestigationService

__all__ = ["InvestigationService", "EntityRead", "RelationshipRead"]
