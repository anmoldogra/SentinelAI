"""case_management domain exceptions.

Module-specific subclasses that REUSE the documented ``api-design.md`` §2.4 error
codes (never invent a new code — that is a contract violation, guide Part 11).
They only make raise-sites more expressive; the HTTP mapping is unchanged.
"""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class CaseNotFoundError(NotFoundError):
    """No case with the given id is visible to the caller."""


class ReportNotFoundError(NotFoundError):
    """No report with the given id exists for the case."""


class ReportNotReadyError(ConflictError):
    """The report exists but has not finished generating, so it has no object to download."""


class EvidenceLinkNotFoundError(NotFoundError):
    """The evidence is not linked to this case."""


class InvalidCaseStatusTransitionError(ConflictError):
    """The requested case status transition is not allowed from the current status."""


class EvidenceAlreadyLinkedError(ConflictError):
    """The evidence is already linked to this case."""
