"""API idempotency — ADR-0012, api-design.md §2.9.

Public surface for the entrypoints that wire it up: the router dependency, the replay signal the
exception handler renders, and the purge job the worker schedules. The repository and the
fingerprint function are internals of this package.
"""

from __future__ import annotations

from sentinelai.platform.idempotency.guard import (
    HEADER_NAME,
    IDEMPOTENT_METHODS,
    REPLAY_HEADER,
    IdempotentReplay,
    enforce_idempotency,
)
from sentinelai.platform.idempotency.jobs import purge_expired_idempotency_keys
from sentinelai.platform.idempotency.models import IdempotencyKey

__all__ = [
    "HEADER_NAME",
    "IDEMPOTENT_METHODS",
    "REPLAY_HEADER",
    "IdempotencyKey",
    "IdempotentReplay",
    "enforce_idempotency",
    "purge_expired_idempotency_keys",
]
