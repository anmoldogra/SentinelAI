"""Key registry — ADR-0009 §1/§7.

Resolves a logical ``KeyRef`` (purpose + name) to a concrete provider + backend key name, and
resolves a ``KeyId`` back to the provider that can verify/decrypt it. Today one config-selected
provider serves all purposes; the interface already supports per-purpose providers (e.g. an HSM
for the evidence root, cloud KMS for storage) with no caller impact.
"""

from __future__ import annotations

from sentinelai.platform.crypto.provider import CryptoProvider
from sentinelai.platform.crypto.types import KeyId, KeyPurpose, KeyRef


def backend_key_name(ref: KeyRef) -> str:
    """Deterministic backend key name encoding the hierarchy position."""
    return f"{ref.purpose.value}__{ref.name}"


class KeyRegistry:
    def __init__(self, default_provider: CryptoProvider) -> None:
        self._default = default_provider
        self._by_purpose: dict[KeyPurpose, CryptoProvider] = {}

    def bind(self, purpose: KeyPurpose, provider: CryptoProvider) -> None:
        """Route a specific purpose to a specific provider (optional; else the default)."""
        self._by_purpose[purpose] = provider

    def resolve(self, ref: KeyRef) -> tuple[CryptoProvider, str]:
        provider = self._by_purpose.get(ref.purpose, self._default)
        return provider, backend_key_name(ref)

    def provider_for(self, key_id: KeyId) -> CryptoProvider:
        for provider in (*self._by_purpose.values(), self._default):
            if provider.kind == key_id.provider:
                return provider
        return self._default

    def providers(self) -> tuple[CryptoProvider, ...]:
        seen: dict[int, CryptoProvider] = {id(self._default): self._default}
        for p in self._by_purpose.values():
            seen[id(p)] = p
        return tuple(seen.values())
