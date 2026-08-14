"""case_management public interface — the ONLY symbols other modules may import.

Cross-module code depends on this, never on ``models.py``/``repository.py``/internals
(guide Part 1). Entrypoint wiring (``router``, ``register_consumers``,
``provide_case_access_checker``) is imported directly by the composition root, which
is allowed to reach into a module — that is not a cross-module dependency.
"""

from __future__ import annotations

from sentinelai.modules.case_management.schemas import CaseRead, CaseReportRead
from sentinelai.modules.case_management.service import CaseService

__all__ = ["CaseRead", "CaseReportRead", "CaseService"]
