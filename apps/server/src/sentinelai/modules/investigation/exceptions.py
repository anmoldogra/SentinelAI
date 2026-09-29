"""investigation domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class EntityNotFoundError(NotFoundError):
    """No entity with the given id exists."""


class RelationshipNotFoundError(NotFoundError):
    """No relationship with the given id exists."""


class CorrelationRunNotFoundError(NotFoundError):
    """No correlation run with the given id exists."""


class FindingAlreadyReviewedError(ConflictError):
    """The entity/relationship has already been dispositioned (not still ``proposed``)."""


class CorrelationRunInProgressError(ConflictError):
    """A correlation run is already queued or running for this case — api-design.md §6's 409.

    Two concurrent runs over one case are not merely wasteful: both walk the same evidence with the
    same extractor, and while entity resolution converges them onto the same nodes, each announces
    its own ``investigation.correlation_generated`` per finding — so the case owner is notified
    twice for one fact and the review queue shows the work of a run nobody asked for.
    """


class CaseNotFoundError(NotFoundError):
    """The case a correlation run was requested for does not exist — api-design.md §6's 404.

    Declared here rather than imported from `case_management`: that module's exception is its
    internal type, not part of its `public.py`, and a 404 is the same 404 whichever module noticed
    the absence. `read_case_evidence_scope` returning ``None`` is how this module learns of it.
    """
