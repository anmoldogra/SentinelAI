"""investigation background jobs — arq (guide Part 12).

The correlation run's ``correlation_runs`` row IS the job's state. A long run updates
progress incrementally and checks ``cancellation_requested`` at safe checkpoints
(cooperative cancellation). Body deferred (``NotImplementedError``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def run_correlation(ctx: dict[str, Any], run_id: UUID) -> None:
    """Execute an AI cross-domain correlation run for a case.

    DEFERRED (external-service dependency, permitted by the phase spec): cross-domain
    correlation is AI/LLM-driven (roadmap Phase 3 "AI Investigation Engine"), and no
    model/inference client exists yet. When built, this job will: mark the run
    ``running``, read the case's eligible evidence, invoke the correlation model,
    persist proposed entities/relationships via ``InvestigationService.create_relationship``
    (each with ≥1 supporting evidence, CEM §13), update ``findings_generated_count``,
    honour ``cancellation_requested`` at checkpoints, and publish
    ``investigation.correlation_run_completed``/``_failed``. The enqueue path (service
    trigger) is fully implemented.
    """
    raise NotImplementedError(
        "correlation is AI/LLM-driven (roadmap Phase 3) — no inference client yet; Phase 8 report"
    )
