"""ingestion public interface — the ONLY symbols other modules may import.

``EvidenceService.exists``, ``read_evidence_attributes`` and ``read_evidence_content`` are the
cross-module read hooks other modules use — ``case_management`` validating an ``evidence_id`` at
link time, ``threat_intel``'s IOC matcher reading the attributes that `event-driven-architecture.md`
§181 deliberately keeps off the event bus, and `investigation`'s correlation run reading a whole
case's evidence content. Always via this interface, never by importing ingestion's repository or
querying its tables (§5).

``EvidenceCreate`` and ``get_evidence_service`` are exported for the **domain-producer** modules
(`osint`, `threat_intel`, `forensics`, `social_media`): `database-design.md` §3.3 gives each of them
an `evidence_id` that is "nullable until the record is published", and publishing means normalizing
their rich record into the CEM. `api-design.md` (e.g. §4.3's OSINT publish) specifies that as a
synchronous `200` carrying the new `evidence_id` plus a custody genesis entry, which an outbox
hand-off cannot produce — the evidence would not exist yet when the response was written.

Exporting the **provider** rather than only the class is what keeps the boundary honest: a sibling
module asks for a configured ``EvidenceService`` and never learns how one is built, so ingestion's
storage and KMS wiring stay ingestion's concern. Constructing one by hand elsewhere would couple
that module to this one's composition, which is the coupling `public.py` exists to prevent.
"""

from __future__ import annotations

from sentinelai.modules.ingestion.schemas import EvidenceCreate, EvidenceRead
from sentinelai.modules.ingestion.service import (
    EvidenceContent,
    EvidenceService,
    get_evidence_service,
    read_evidence_attributes,
    read_evidence_content,
)

__all__ = [
    "EvidenceContent",
    "EvidenceCreate",
    "EvidenceRead",
    "EvidenceService",
    "get_evidence_service",
    "read_evidence_attributes",
    "read_evidence_content",
]
