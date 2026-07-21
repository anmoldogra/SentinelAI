"""case_management background jobs — arq (guide Part 12).

Report generation is async: ``POST /cases/{id}/reports`` enqueues this job and the
``case_reports`` row is the job's state (``api-design.md`` §2.12/§7). On completion
the job publishes ``case.report_generated`` via the module outbox. Registered in the
worker's ``WorkerSettings.functions``. Body deferred (``NotImplementedError``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


async def generate_case_report(ctx: dict[str, Any], case_id: UUID) -> None:
    """Build the case report (structured JSON) and persist a ``case_reports`` row.

    DEFERRED — blocked on two documented prerequisites, not on this module:
    (1) ``platform/storage.py`` (object storage client) does not exist yet, so the
        rendered JSON cannot be written and no ``storage_ref`` can be produced; and
    (2) the ``case_reports`` schema (database-design.md §3.4) has NOT-NULL
        ``storage_ref``/``generated_at`` and no ``status`` column, so a pending row
        cannot be inserted before the report completes — this contradiction with the
        async job-state-row pattern (guide Part 12) needs a docs decision first.
    Both are flagged in the Phase 7 report; the enqueue path (service trigger) works.
    """
    raise NotImplementedError(
        "report rendering is blocked on platform/storage.py and a case_reports schema "
        "decision (see database-design.md §3.4) — Phase 7 report"
    )
