"""threat_intel public interface — the ONLY symbols other modules may import.

``read_ioc`` is the cross-module read hook: `threat_intel.ioc_matched` carries the IOC's *id* and
type, not its value (§25's payload schema), so `investigation` fetches the indicator it needs to
name a graph entity through here rather than through this module's repository or tables (§5).
"""

from __future__ import annotations

from sentinelai.modules.threat_intel.schemas import IocRead
from sentinelai.modules.threat_intel.service import ThreatIntelService, read_ioc

__all__ = ["IocRead", "ThreatIntelService", "read_ioc"]
