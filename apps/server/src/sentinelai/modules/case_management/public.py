"""case_management public interface — the ONLY symbols other modules may import.

Cross-module code depends on this, never on ``models.py``/``repository.py``/internals
(guide Part 1). Entrypoint wiring (``router``, ``register_consumers``,
``provide_case_access_checker``) is imported directly by the composition root, which
is allowed to reach into a module — that is not a cross-module dependency.

``read_cases_for_evidence`` is the cross-module read hook: no evidence-bearing event carries a
case, because the link is this module's fact and can change after the event, so a consumer acting
"for the case owning the matched evidence" (event-driven §25.8) asks here rather than joining across
schemas (§5). ``read_case_evidence_scope`` is the same hook in the other direction, for
`investigation`'s correlation run, which needs a case's whole linked-evidence set plus its owner.
"""

from __future__ import annotations

from sentinelai.modules.case_management.schemas import CaseRead, CaseReportRead
from sentinelai.modules.case_management.service import (
    CaseEvidenceRef,
    CaseEvidenceScope,
    CaseService,
    read_case_evidence_scope,
    read_cases_for_evidence,
)

__all__ = [
    "CaseEvidenceRef",
    "CaseEvidenceScope",
    "CaseRead",
    "CaseReportRead",
    "CaseService",
    "read_case_evidence_scope",
    "read_cases_for_evidence",
]
