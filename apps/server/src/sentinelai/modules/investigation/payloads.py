"""Defensive event-payload parsing, shared by this module's consumers and projectors.

**Why a consumer parses defensively instead of trusting the envelope.** The fact an event describes
already happened on the write side, and a handler that raised over a malformed field would
dead-letter an event describing something true — then block its aggregate's whole queue under
ADR-0006's per-aggregate ordering. A skipped projection is logged and converges on the next rebuild;
a dead-lettered event is not replayed without an operator.

So these return ``None`` rather than raising, and every caller decides what a missing field means.
They are deliberately not validators: a payload that fails here is a publisher bug, and the honest
response is to record that one event did nothing, not to invent a value that lets it half-succeed.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from uuid import UUID

__all__ = ["payload_decimal", "payload_uuid"]


def payload_uuid(value: object) -> UUID | None:
    """A payload id as a ``UUID``, or ``None`` for anything unusable."""
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def payload_decimal(value: object) -> Decimal | None:
    """A payload confidence as a ``Decimal``, or ``None`` for anything unusable.

    ``Decimal`` and not ``float``: a confidence is compared against `api-design.md` §6's
    ``min_confidence`` threshold, and binary floating point would answer the boundary case
    differently here than the ``numeric`` column does on the write side (ADR-0011 §2). Events carry
    it as a string for exactly that reason, so parsing it as a float and back would throw away the
    precision the publisher took care to keep.
    """
    if isinstance(value, Decimal):
        return value
    if not isinstance(value, str | int):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None
