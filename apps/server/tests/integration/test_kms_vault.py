"""Vault Transit provider contract test (ADR-0009).

Requires a running Vault with the Transit engine enabled. Skips unless
``VAULT_TEST_ADDR`` + ``VAULT_TEST_TOKEN`` are set, so it never fails a keyless CI run while
still providing a real backend contract check when infrastructure is available.
"""

from __future__ import annotations

import os

import pytest

from sentinelai.platform.crypto.backends.vault import VaultTransitProvider
from sentinelai.platform.crypto.types import Algorithm, KeyId, KeyPurpose, ProviderKind

_ADDR = os.getenv("VAULT_TEST_ADDR")


async def test_vault_transit_sign_verify_and_health() -> None:
    if not _ADDR or not os.getenv("VAULT_TEST_TOKEN"):
        pytest.skip("set VAULT_TEST_ADDR + VAULT_TEST_TOKEN to run the Vault contract test")
    provider = VaultTransitProvider(
        addr=_ADDR,
        mount=os.getenv("VAULT_TEST_MOUNT", "transit"),
        token=os.environ["VAULT_TEST_TOKEN"],
    )
    key = "evidence_root__contracttest"
    try:
        await provider.start()
        assert (await provider.health()).state.value in {"ready", "degraded"}
        await provider.create_key(key, KeyPurpose.EVIDENCE_ROOT, Algorithm.ED25519)
        version = await provider.current_version(key)
        value = await provider.sign_bytes(key, Algorithm.ED25519, version, b"contract-message")
        key_id = KeyId(ProviderKind.VAULT_TRANSIT, key, version)
        assert (
            await provider.verify_bytes(b"contract-message", value, Algorithm.ED25519, key_id)
            is True
        )
        assert await provider.verify_bytes(b"tampered", value, Algorithm.ED25519, key_id) is False
        rotated = await provider.rotate(key)
        assert rotated.version == version + 1
    finally:
        await provider.aclose()
