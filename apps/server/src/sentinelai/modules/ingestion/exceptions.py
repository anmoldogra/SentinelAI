"""ingestion domain exceptions — reuse documented api-design.md §2.4 codes."""

from __future__ import annotations

from sentinelai.shared.exceptions import ConflictError, NotFoundError


class EvidenceNotFoundError(NotFoundError):
    """No evidence with the given id is visible to the caller."""


class ConnectorNotFoundError(NotFoundError):
    """No connector with the given id is registered."""


class EvidencePayloadMissingError(NotFoundError):
    """The evidence row references a stored payload that is not present in object storage."""


class EvidenceAlreadySupersededError(ConflictError):
    """The evidence has already been superseded by a later version."""


class IntegrityVerificationFailedError(ConflictError):
    """The payload's server-recomputed digest does not match the recorded integrity hash.

    Reuses the documented ``CONFLICT`` code (api-design.md §2.4) — the stored bytes conflict with
    the recorded state. Deliberately carries no digest values: the mismatch is recorded on the
    custody ledger, not leaked through the API error body.
    """
