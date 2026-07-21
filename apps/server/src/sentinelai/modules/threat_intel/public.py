"""threat_intel public interface — the ONLY symbols other modules may import."""

from __future__ import annotations

from sentinelai.modules.threat_intel.schemas import IocRead
from sentinelai.modules.threat_intel.service import ThreatIntelService

__all__ = ["ThreatIntelService", "IocRead"]
