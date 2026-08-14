"""ingestion public interface — the ONLY symbols other modules may import.

``EvidenceService.exists`` is the cross-module hook other modules use (e.g.
``case_management`` validating an ``evidence_id`` at link time) — always via this
interface, never by importing ingestion's repository or querying its tables (§5).
"""

from __future__ import annotations

from sentinelai.modules.ingestion.schemas import EvidenceRead
from sentinelai.modules.ingestion.service import EvidenceService

__all__ = ["EvidenceRead", "EvidenceService"]
