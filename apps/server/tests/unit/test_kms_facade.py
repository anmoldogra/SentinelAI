"""Unit tests for the KMS facade, registry, and provider construction (ADR-0009).

Tier-0 surface: these cover the lifecycle/routing/health paths the dev-provider tests do not
reach, plus `build_provider`'s fail-closed production guards. The facade is driven against the
real ``DevKmsProvider`` (an ephemeral tmp keystore) rather than a mock, so the behaviour under
test is real crypto routing, not a stub's script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sentinelai.platform.config import Settings
from sentinelai.platform.crypto.audit import StructlogAuditSink
from sentinelai.platform.crypto.backends.dev import DevKmsProvider
from sentinelai.platform.crypto.backends.vault import VaultTransitProvider
from sentinelai.platform.crypto.exceptions import (
    AlgorithmNotAllowed,
    CapabilityNotSupported,
    InsecureConfiguration,
    ProviderNotAvailable,
)
from sentinelai.platform.crypto.kms import (
    KeyManagementService,
    build_provider,
    create_kms,
)
from sentinelai.platform.crypto.policy import AlgorithmPolicy
from sentinelai.platform.crypto.registry import KeyRegistry, backend_key_name
from sentinelai.platform.crypto.types import (
    Algorithm,
    HealthState,
    HealthStatus,
    KeyId,
    KeyPurpose,
    KeyRef,
    ProviderKind,
)

_REF = KeyRef(purpose=KeyPurpose.ROOT_TRUST, name="default")


@pytest.fixture
def kms(tmp_path: Path) -> KeyManagementService:
    """A facade over a real dev provider with an ephemeral keystore."""
    provider = DevKmsProvider(str(tmp_path / "keystore"), is_production=False)
    return KeyManagementService(
        KeyRegistry(provider),
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )


class _StubProvider:
    """Minimal provider used only where a *specific* health/capability shape is needed."""

    def __init__(self, kind: ProviderKind, state: HealthState) -> None:
        self.kind = kind
        self._state = state
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True

    async def health(self) -> HealthStatus:
        return HealthStatus(self._state, self.kind)


# --- registry routing -------------------------------------------------------


def test_backend_key_names_encode_the_hierarchy_position() -> None:
    assert backend_key_name(_REF) == "root_trust__default"


def test_a_purpose_can_be_bound_to_a_specific_provider() -> None:
    default = _StubProvider(ProviderKind.DEV, HealthState.READY)
    special = _StubProvider(ProviderKind.VAULT_TRANSIT, HealthState.READY)
    registry = KeyRegistry(default)  # type: ignore[arg-type]
    registry.bind(KeyPurpose.STORAGE_ROOT, special)  # type: ignore[arg-type]

    assert registry.resolve(_REF)[0] is default  # unbound purpose -> default
    bound = KeyRef(purpose=KeyPurpose.STORAGE_ROOT, name="default")
    assert registry.resolve(bound)[0] is special


def test_a_key_id_routes_back_to_the_provider_that_issued_it() -> None:
    default = _StubProvider(ProviderKind.DEV, HealthState.READY)
    special = _StubProvider(ProviderKind.VAULT_TRANSIT, HealthState.READY)
    registry = KeyRegistry(default)  # type: ignore[arg-type]
    registry.bind(KeyPurpose.STORAGE_ROOT, special)  # type: ignore[arg-type]

    vault_key = KeyId(ProviderKind.VAULT_TRANSIT, "k", 1)
    assert registry.provider_for(vault_key) is special
    assert registry.provider_for(KeyId(ProviderKind.DEV, "k", 1)) is default


def test_an_unknown_provider_kind_falls_back_to_the_default() -> None:
    default = _StubProvider(ProviderKind.DEV, HealthState.READY)
    registry = KeyRegistry(default)  # type: ignore[arg-type]
    assert registry.provider_for(KeyId(ProviderKind.AWS_KMS, "k", 1)) is default


def test_providers_are_deduplicated() -> None:
    shared = _StubProvider(ProviderKind.DEV, HealthState.READY)
    registry = KeyRegistry(shared)  # type: ignore[arg-type]
    registry.bind(KeyPurpose.STORAGE_ROOT, shared)  # type: ignore[arg-type]
    assert len(registry.providers()) == 1  # the same object is not started/closed twice


# --- facade lifecycle -------------------------------------------------------


async def test_start_and_aclose_propagate_to_every_provider() -> None:
    provider = _StubProvider(ProviderKind.DEV, HealthState.READY)
    kms = KeyManagementService(
        KeyRegistry(provider),  # type: ignore[arg-type]
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    await kms.start()
    await kms.aclose()
    assert (provider.started, provider.closed) == (True, True)


async def test_a_provider_without_lifecycle_hooks_is_skipped() -> None:
    class _Bare:
        kind = ProviderKind.DEV

        async def health(self) -> HealthStatus:
            return HealthStatus(HealthState.READY, self.kind)

    kms = KeyManagementService(
        KeyRegistry(_Bare()),  # type: ignore[arg-type]
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    await kms.start()  # must not raise on a provider with no start/aclose
    await kms.aclose()


# --- aggregate health -------------------------------------------------------


async def test_health_is_ready_when_every_provider_is_ready() -> None:
    kms = KeyManagementService(
        KeyRegistry(_StubProvider(ProviderKind.DEV, HealthState.READY)),  # type: ignore[arg-type]
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    assert (await kms.health()).state is HealthState.READY


async def test_one_unavailable_provider_makes_the_whole_kms_unavailable() -> None:
    """Fail closed: a signing subsystem is only as available as its worst provider."""
    registry = KeyRegistry(_StubProvider(ProviderKind.DEV, HealthState.READY))  # type: ignore[arg-type]
    registry.bind(
        KeyPurpose.STORAGE_ROOT,
        _StubProvider(ProviderKind.VAULT_TRANSIT, HealthState.UNAVAILABLE),  # type: ignore[arg-type]
    )
    kms = KeyManagementService(
        registry,
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    assert (await kms.health()).state is HealthState.UNAVAILABLE


async def test_a_degraded_provider_downgrades_an_otherwise_ready_kms() -> None:
    registry = KeyRegistry(_StubProvider(ProviderKind.DEV, HealthState.READY))  # type: ignore[arg-type]
    registry.bind(
        KeyPurpose.STORAGE_ROOT,
        _StubProvider(ProviderKind.VAULT_TRANSIT, HealthState.DEGRADED),  # type: ignore[arg-type]
    )
    kms = KeyManagementService(
        registry,
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    assert (await kms.health()).state is HealthState.DEGRADED


# --- auditable key lifecycle ------------------------------------------------


async def test_create_then_read_metadata(kms: KeyManagementService) -> None:
    created = await kms.create_key(_REF)
    assert created.key_id.version >= 1
    assert (await kms.get_metadata(_REF)).key_id.backend_ref == created.key_id.backend_ref


async def test_rotation_advances_the_version_and_lists_both(kms: KeyManagementService) -> None:
    await kms.create_key(_REF)
    before = (await kms.get_metadata(_REF)).key_id.version
    rotated = await kms.rotate(_REF)
    assert rotated.version == before + 1
    assert await kms.list_versions(_REF) == tuple(range(1, rotated.version + 1))


@pytest.mark.parametrize("operation", ["disable", "enable", "archive"])
async def test_lifecycle_operations_apply_to_the_resolved_key(
    kms: KeyManagementService, operation: str
) -> None:
    await kms.create_key(_REF)
    await getattr(kms, operation)(_REF)  # each records an audit entry and must not raise


async def test_a_public_key_is_exportable_for_a_signing_key(kms: KeyManagementService) -> None:
    metadata = await kms.create_key(_REF)
    assert await kms.public_key(metadata.key_id)


# --- encryption / data keys -------------------------------------------------


async def test_encrypt_decrypt_round_trip(kms: KeyManagementService) -> None:
    ref = KeyRef(purpose=KeyPurpose.STORAGE_ROOT, name="default")
    await kms.create_key(ref)
    ciphertext = await kms.encrypt(ref, b"evidence bytes")
    assert await kms.decrypt(ciphertext) == b"evidence bytes"


async def test_aad_must_match_on_decrypt(kms: KeyManagementService) -> None:
    ref = KeyRef(purpose=KeyPurpose.STORAGE_ROOT, name="default")
    await kms.create_key(ref)
    ciphertext = await kms.encrypt(ref, b"payload", b"case-42")
    with pytest.raises(Exception):  # noqa: B017 - any failure is acceptable; success is not
        await kms.decrypt(ciphertext, b"case-99")


async def test_a_data_key_is_returned_in_plaintext_and_wrapped_form(
    kms: KeyManagementService,
) -> None:
    ref = KeyRef(purpose=KeyPurpose.STORAGE_ROOT, name="default")
    await kms.create_key(ref)
    data_key = await kms.generate_data_key(ref)
    assert data_key.plaintext and data_key.wrapped


async def test_a_provider_without_data_key_support_is_refused() -> None:
    class _NoDataKeys(_StubProvider):
        def capabilities(self) -> Any:
            class _Caps:
                data_keys = False

            return _Caps()

    kms = KeyManagementService(
        KeyRegistry(_NoDataKeys(ProviderKind.DEV, HealthState.READY)),  # type: ignore[arg-type]
        AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False),
        StructlogAuditSink(),
    )
    with pytest.raises(CapabilityNotSupported):
        await kms.generate_data_key(KeyRef(purpose=KeyPurpose.STORAGE_ROOT, name="d"))


# --- provider construction (fail-closed production guards) ------------------


def test_the_dev_provider_is_built_outside_production(tmp_path: Path) -> None:
    cfg = Settings(kms_provider="dev", kms_dev_keystore=str(tmp_path), app_env="development")
    assert isinstance(build_provider(cfg), DevKmsProvider)


def test_a_registered_but_unimplemented_provider_slot_fails_closed() -> None:
    cfg = Settings(kms_provider="aws_kms", app_env="development")
    with pytest.raises(ProviderNotAvailable, match="not yet implemented"):
        build_provider(cfg)


def test_vault_is_built_without_credential_checks_outside_production() -> None:
    cfg = Settings(kms_provider="vault_transit", app_env="development")
    assert isinstance(build_provider(cfg), VaultTransitProvider)


@pytest.mark.parametrize("token", ["", "dev-only-change-me", "root", "dev-only-token"])
def test_a_placeholder_vault_token_is_refused_in_production(token: str) -> None:
    """M6: a national evidence platform must never sign under a well-known token."""
    cfg = Settings(
        kms_provider="vault_transit",
        app_env="production",
        vault_auth_method="token",
        vault_token=token,  # type: ignore[arg-type]
        database_url="postgresql+asyncpg://u:p@db:5432/s",
    )
    with pytest.raises(InsecureConfiguration, match="VAULT_TOKEN"):
        build_provider(cfg)


def test_approle_without_credentials_is_refused_in_production() -> None:
    cfg = Settings(
        kms_provider="vault_transit",
        app_env="production",
        vault_auth_method="approle",
        database_url="postgresql+asyncpg://u:p@db:5432/s",
    )
    with pytest.raises(InsecureConfiguration, match="AppRole"):
        build_provider(cfg)


def test_an_unsupported_vault_auth_method_is_refused_in_production() -> None:
    cfg = Settings(
        kms_provider="vault_transit",
        app_env="production",
        vault_auth_method="kerberos",
        database_url="postgresql+asyncpg://u:p@db:5432/s",
    )
    with pytest.raises(InsecureConfiguration, match="VAULT_AUTH_METHOD"):
        build_provider(cfg)


def test_a_real_production_vault_token_is_accepted() -> None:
    cfg = Settings(
        kms_provider="vault_transit",
        app_env="production",
        vault_auth_method="token",
        vault_token="hvs.a-real-injected-token",  # type: ignore[arg-type]
        database_url="postgresql+asyncpg://u:p@db:5432/s",
    )
    assert isinstance(build_provider(cfg), VaultTransitProvider)


def test_create_kms_assembles_a_usable_facade(tmp_path: Path) -> None:
    cfg = Settings(kms_provider="dev", kms_dev_keystore=str(tmp_path), app_env="development")
    assert isinstance(create_kms(cfg), KeyManagementService)


# --- policy -----------------------------------------------------------------


def test_hybrid_signing_is_accepted_and_still_yields_a_usable_bundle() -> None:
    """ADR-0009 §5: the bundle grows with a PQC scheme later, without any caller/API change —
    today it is still the single configured algorithm."""
    hybrid = AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=True)
    assert hybrid.signing_algorithms(KeyPurpose.ROOT_TRUST) == (Algorithm.ED25519,)


def test_an_unknown_signing_algorithm_is_refused() -> None:
    with pytest.raises(AlgorithmNotAllowed):
        AlgorithmPolicy.from_config(signing_algorithm="ROT13", hybrid=False)


def test_an_encryption_algorithm_cannot_be_used_for_signing() -> None:
    with pytest.raises(AlgorithmNotAllowed):
        AlgorithmPolicy.from_config(signing_algorithm="AES_256_GCM", hybrid=False)


def test_encryption_uses_the_configured_symmetric_algorithm() -> None:
    policy = AlgorithmPolicy.from_config(signing_algorithm="ED25519", hybrid=False)
    assert policy.encryption_algorithm(KeyPurpose.STORAGE_ROOT) is Algorithm.AES_256_GCM
