"""``CryptoProvider`` port — ADR-0009 (revised for C1 + H1).

Signing is now a **raw byte** operation: the facade builds and owns the self-authenticating
``SignedHeader``; providers only sign/verify opaque bytes with a version-pinned key (so a
concurrent rotation cannot make a signature disagree with its own header). ``start()`` lets a
provider begin background work (e.g. Vault lease renewal). No provider-specific type escapes
``platform.crypto``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sentinelai.platform.crypto.types import (
    Algorithm,
    Ciphertext,
    DataKey,
    HealthStatus,
    KeyId,
    KeyMetadata,
    KeyPurpose,
    ProviderCapabilities,
    ProviderKind,
)


@runtime_checkable
class CryptoProvider(Protocol):
    kind: ProviderKind

    def capabilities(self) -> ProviderCapabilities: ...

    async def start(self) -> None: ...

    async def health(self) -> HealthStatus: ...

    # -- signing (raw bytes; facade owns the SignedHeader) -----------------------
    async def current_version(self, key: str) -> int: ...

    async def sign_bytes(
        self, key: str, algorithm: Algorithm, version: int, data: bytes
    ) -> bytes: ...

    async def verify_bytes(
        self, data: bytes, signature: bytes, algorithm: Algorithm, key_id: KeyId
    ) -> bool: ...

    # -- encryption / data keys --------------------------------------------------
    async def encrypt(
        self, key: str, algorithm: Algorithm, plaintext: bytes, aad: bytes | None
    ) -> Ciphertext: ...

    async def decrypt(self, ciphertext: Ciphertext, aad: bytes | None) -> bytes: ...

    async def generate_data_key(self, key: str) -> DataKey: ...

    async def public_key(self, key_id: KeyId) -> bytes: ...

    # -- auditable key lifecycle -------------------------------------------------
    async def create_key(
        self, key: str, purpose: KeyPurpose, algorithm: Algorithm
    ) -> KeyMetadata: ...

    async def rotate(self, key: str) -> KeyId: ...

    async def enable(self, key: str) -> None: ...

    async def disable(self, key: str) -> None: ...

    async def archive(self, key: str) -> None: ...

    async def destroy(self, key: str) -> None: ...

    async def list_versions(self, key: str) -> tuple[int, ...]: ...

    async def get_metadata(self, key: str) -> KeyMetadata: ...
