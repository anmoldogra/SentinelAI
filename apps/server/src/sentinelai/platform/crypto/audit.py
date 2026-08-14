"""KMS lifecycle audit sink — ADR-0009 §6.

Key lifecycle operations (create/enable/disable/rotate/archive/destroy) are themselves audit
events. The sink is a port so KMS does not couple to ``platform.auth``'s audit ledger: today
it records via structured logging; the hash-chained ledger (ADR-0003) can be injected later
without changing KMS. Records never contain key material.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sentinelai.platform.crypto.types import KeyId, KeyPurpose, ProviderKind
from sentinelai.platform.logging import log


@runtime_checkable
class AuditSink(Protocol):
    async def record(
        self,
        *,
        operation: str,
        provider: ProviderKind,
        purpose: KeyPurpose | None = None,
        key_id: KeyId | None = None,
        result: str = "success",
        detail: str | None = None,
    ) -> None: ...


class StructlogAuditSink:
    """Default sink — structured logging. No key material is ever included."""

    async def record(
        self,
        *,
        operation: str,
        provider: ProviderKind,
        purpose: KeyPurpose | None = None,
        key_id: KeyId | None = None,
        result: str = "success",
        detail: str | None = None,
    ) -> None:
        log.info(
            "kms_key_lifecycle",
            operation=operation,
            provider=str(provider),
            purpose=str(purpose) if purpose else None,
            key_backend_ref=key_id.backend_ref if key_id else None,
            key_version=key_id.version if key_id else None,
            result=result,
            detail=detail,
        )
