"""investigation's graph read models — ADR-0013.

The read half of the module: projection tables in the ``investigation_read`` schema, the projectors
that maintain them from integration events, and the depth-bounded traversal that serves
``GET /cases/{case_id}/graph``.

Nothing outside ``investigation`` imports from here. The projection is this module's view of its own
aggregates; another module wanting graph data goes through the HTTP endpoint or
``investigation.public``, as it would for any other of this module's data.
"""

from __future__ import annotations

from sentinelai.modules.investigation.read.models import (
    SCHEMA,
    CaseGraphEdge,
    CaseGraphNode,
)
from sentinelai.modules.investigation.read.projector import (
    project_correlation_generated,
    project_finding_reviewed,
)
from sentinelai.modules.investigation.read.repository import (
    MAX_DEPTH,
    GraphProjectionRepository,
)

__all__ = [
    "MAX_DEPTH",
    "SCHEMA",
    "CaseGraphEdge",
    "CaseGraphNode",
    "GraphProjectionRepository",
    "project_correlation_generated",
    "project_finding_reviewed",
]
