"""Unit tests for the Vault Transit KMS provider (ADR-0009).

Driven against an in-process fake Vault via ``httpx.MockTransport``: real request construction,
real header/auth handling, real status-code translation — no network, no Vault installation, fully
deterministic. The live-Vault contract test (``tests/integration/test_kms_vault.py``) proves the
same provider against the real daemon; these cover the error and lifecycle paths that a healthy
Vault never exercises.

Only the transport is substituted. The provider's own logic is never mocked.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest

from sentinelai.platform.crypto.backends.vault import VaultTransitProvider
from sentinelai.platform.crypto.exceptions import (
    CapabilityNotSupported,
    CryptoError,
    KeyNotFound,
    KmsUnavailable,
)
from sentinelai.platform.crypto.resilience import RetryPolicy
from sentinelai.platform.crypto.types import (
    Algorithm,
    Ciphertext,
    HealthState,
    KeyId,
    KeyPurpose,
    ProviderKind,
)

_MOUNT = "transit"
_FAST = RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0, timeout=1.0)


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse every delay to zero while still yielding, so background tasks make progress."""
    real_sleep = asyncio.sleep

    async def _instant(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _instant)


class _FakeVault:
    """Routes clamd-free, deterministic responses for the endpoints the provider calls."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        self.default: tuple[int, dict[str, Any]] = (200, {"data": {}})

    def route(self, method: str, path: str, status: int, body: dict[str, Any]) -> None:
        self.routes[(method, path)] = (status, body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.routes.get((request.method, request.url.path), self.default)
        return httpx.Response(status, content=json.dumps(body), headers={"x-t": "json"})


def _provider(vault: _FakeVault, **overrides: Any) -> VaultTransitProvider:
    kwargs: dict[str, Any] = {
        "addr": "http://vault.test",
        "mount": _MOUNT,
        "token": "root-token",
        "retry": _FAST,
    }
    kwargs.update(overrides)
    provider = VaultTransitProvider(**kwargs)
    # Substitute only the transport; every layer above it is the real implementation.
    provider._client = httpx.AsyncClient(
        base_url="http://vault.test", transport=httpx.MockTransport(vault.handler)
    )
    return provider


def _key_read_body(*, latest: int = 2, key_type: str = "ed25519", public: str = "pk") -> dict:
    return {
        "data": {
            "latest_version": latest,
            "type": key_type,
            "keys": {str(v): {"public_key": public} for v in range(1, latest + 1)},
        }
    }


def _vault_token(version: int, raw: bytes) -> str:
    return f"vault:v{version}:{base64.b64encode(raw).decode()}"


# --- identity / capabilities ------------------------------------------------


def test_capabilities_advertise_the_transit_surface() -> None:
    caps = _provider(_FakeVault()).capabilities()
    assert caps.kind is ProviderKind.VAULT_TRANSIT
    assert (caps.signing, caps.encryption, caps.data_keys, caps.rotation) == (
        True,
        True,
        True,
        True,
    )
    assert caps.attestation is False  # transit is software-backed, not an HSM
    assert Algorithm.ED25519 in caps.algorithms


# --- auth + lease lifecycle (H1) -------------------------------------------


async def test_token_start_discovers_lease_renewability() -> None:
    vault = _FakeVault()
    vault.route(
        "GET", "/v1/auth/token/lookup-self", 200, {"data": {"ttl": 3600, "renewable": False}}
    )
    provider = _provider(vault)
    await provider.start()
    assert provider._renewable is False
    assert provider._renew_task is None  # nothing to renew
    await provider.aclose()


async def test_a_failed_lookup_self_is_non_fatal() -> None:
    """A token may still work even if lookup-self is denied — start must not fail closed here."""
    vault = _FakeVault()
    vault.route("GET", "/v1/auth/token/lookup-self", 403, {"errors": ["permission denied"]})
    provider = _provider(vault)
    await provider.start()
    assert provider._renewable is False
    await provider.aclose()


async def test_approle_login_exchanges_credentials_for_a_client_token() -> None:
    vault = _FakeVault()
    vault.route(
        "POST",
        "/v1/auth/approle/login",
        200,
        {"auth": {"client_token": "s.issued", "lease_duration": 600, "renewable": False}},
    )
    provider = _provider(vault, auth_method="approle", role_id="r", secret_id="s", token="ignored")
    await provider.start()
    assert provider._token == "s.issued"
    assert provider._lease_seconds == 600
    await provider.aclose()


async def test_approle_without_credentials_fails_closed() -> None:
    provider = _provider(_FakeVault(), auth_method="approle")
    with pytest.raises(KmsUnavailable, match="role_id"):
        await provider.start()
    await provider.aclose()


async def test_a_rejected_approle_login_fails_closed() -> None:
    vault = _FakeVault()
    vault.route("POST", "/v1/auth/approle/login", 400, {"errors": ["bad role"]})
    provider = _provider(vault, auth_method="approle", role_id="r", secret_id="s")
    with pytest.raises(KmsUnavailable, match="login failed"):
        await provider.start()
    await provider.aclose()


async def test_an_unreachable_vault_during_approle_login_fails_closed() -> None:
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    provider = _provider(_FakeVault(), auth_method="approle", role_id="r", secret_id="s")
    provider._client = httpx.AsyncClient(
        base_url="http://vault.test", transport=httpx.MockTransport(_boom)
    )
    with pytest.raises(KmsUnavailable, match="unreachable"):
        await provider.start()
    await provider.aclose()


async def test_namespace_is_sent_on_every_request() -> None:
    vault = _FakeVault()
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 200, _key_read_body())
    provider = _provider(vault, namespace="team-a")
    await provider.get_metadata("k")
    assert vault.requests[-1].headers["X-Vault-Namespace"] == "team-a"
    assert vault.requests[-1].headers["X-Vault-Token"] == "root-token"
    await provider.aclose()


async def test_a_renewable_token_starts_the_renewal_loop_and_aclose_cancels_it() -> None:
    vault = _FakeVault()
    vault.route("GET", "/v1/auth/token/lookup-self", 200, {"data": {"ttl": 60, "renewable": True}})
    provider = _provider(vault)
    await provider.start()
    assert provider._renew_task is not None
    await provider.aclose()
    assert provider._renew_task.cancelled() or provider._renew_task.done()


async def test_lease_renewal_failure_marks_the_provider_unhealthy() -> None:
    """Fail closed (H1): a lost lease must surface as UNAVAILABLE, not keep signing."""
    vault = _FakeVault()
    vault.route("POST", "/v1/auth/token/renew-self", 403, {"errors": ["denied"]})
    provider = _provider(vault)
    provider._lease_seconds = 0

    task = asyncio.create_task(provider._renewal_loop())
    for _ in range(200):  # let the loop run one iteration
        await asyncio.sleep(0)
        if provider._healthy is False:
            break
    task.cancel()
    assert provider._healthy is False
    assert (await provider.health()).state is HealthState.UNAVAILABLE
    await provider.aclose()


# --- health -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, HealthState.READY),
        (429, HealthState.DEGRADED),  # standby / rate-limited
        (472, HealthState.DEGRADED),  # DR secondary
        (473, HealthState.DEGRADED),  # performance standby
        (501, HealthState.UNAVAILABLE),  # sealed / not initialised
    ],
)
async def test_health_maps_vault_status_codes(status: int, expected: HealthState) -> None:
    vault = _FakeVault()
    vault.route("GET", "/v1/sys/health", status, {})
    provider = _provider(vault)
    assert (await provider.health()).state is expected
    await provider.aclose()


async def test_health_reports_unavailable_when_vault_is_unreachable() -> None:
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = _provider(_FakeVault())
    provider._client = httpx.AsyncClient(
        base_url="http://vault.test", transport=httpx.MockTransport(_boom)
    )
    assert (await provider.health()).state is HealthState.UNAVAILABLE
    await provider.aclose()


# --- status-code translation (the retry/deterministic boundary) -------------


async def test_404_becomes_key_not_found_and_is_not_retried() -> None:
    vault = _FakeVault()
    vault.route("GET", f"/v1/{_MOUNT}/keys/missing", 404, {"errors": []})
    provider = _provider(vault)
    with pytest.raises(KeyNotFound):
        await provider.get_metadata("missing")
    assert len(vault.requests) == 1  # deterministic error: exactly one attempt
    await provider.aclose()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_status_codes_are_retried_then_fail_closed(status: int) -> None:
    vault = _FakeVault()
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", status, {"errors": []})
    provider = _provider(vault)
    with pytest.raises(KmsUnavailable):
        await provider.get_metadata("k")
    assert len(vault.requests) == _FAST.max_attempts
    await provider.aclose()


async def test_a_client_error_is_deterministic_and_not_retried() -> None:
    vault = _FakeVault()
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 403, {"errors": ["denied"]})
    provider = _provider(vault)
    with pytest.raises(CryptoError):
        await provider.get_metadata("k")
    assert len(vault.requests) == 1
    await provider.aclose()


# --- key lifecycle ----------------------------------------------------------


async def test_create_key_rejects_an_algorithm_transit_cannot_hold() -> None:
    provider = _provider(_FakeVault())
    with pytest.raises(CapabilityNotSupported):
        await provider.create_key("k", KeyPurpose.ROOT_TRUST, Algorithm.ML_DSA_65)
    await provider.aclose()


async def test_create_key_posts_the_mapped_vault_key_type() -> None:
    vault = _FakeVault()
    vault.route("POST", f"/v1/{_MOUNT}/keys/k", 200, {"data": {}})
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 200, _key_read_body(key_type="ed25519"))
    provider = _provider(vault)
    metadata = await provider.create_key("k", KeyPurpose.ROOT_TRUST, Algorithm.ED25519)
    created = next(r for r in vault.requests if r.method == "POST")
    assert json.loads(created.content)["type"] == "ed25519"
    assert metadata.algorithm is Algorithm.ED25519
    assert metadata.key_id.version == 2
    await provider.aclose()


async def test_rotate_returns_the_new_current_key_id() -> None:
    vault = _FakeVault()
    vault.route("POST", f"/v1/{_MOUNT}/keys/k/rotate", 200, {"data": {}})
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 200, _key_read_body(latest=3))
    provider = _provider(vault)
    assert (await provider.rotate("k")).version == 3
    await provider.aclose()


@pytest.mark.parametrize("operation", ["enable", "disable", "archive"])
async def test_config_operations_post_to_the_key_config_endpoint(operation: str) -> None:
    vault = _FakeVault()
    vault.route("POST", f"/v1/{_MOUNT}/keys/k/config", 200, {"data": {}})
    provider = _provider(vault)
    await getattr(provider, operation)("k")
    assert vault.requests[-1].url.path == f"/v1/{_MOUNT}/keys/k/config"
    await provider.aclose()


async def test_destroy_allows_deletion_then_deletes_the_key() -> None:
    vault = _FakeVault()
    vault.route("POST", f"/v1/{_MOUNT}/keys/k/config", 200, {"data": {}})
    vault.route("DELETE", f"/v1/{_MOUNT}/keys/k", 200, {"data": {}})
    provider = _provider(vault)
    await provider.destroy("k")
    assert [r.method for r in vault.requests] == ["POST", "DELETE"]
    await provider.aclose()


async def test_list_versions_returns_sorted_integers() -> None:
    vault = _FakeVault()
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 200, _key_read_body(latest=3))
    provider = _provider(vault)
    assert await provider.list_versions("k") == (1, 2, 3)
    await provider.aclose()


async def test_current_version_reads_the_latest() -> None:
    vault = _FakeVault()
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 200, _key_read_body(latest=4))
    provider = _provider(vault)
    assert await provider.current_version("k") == 4
    await provider.aclose()


async def test_metadata_reports_archived_when_deletion_is_allowed() -> None:
    vault = _FakeVault()
    body = _key_read_body()
    body["data"]["deletion_allowed"] = True
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 200, body)
    provider = _provider(vault)
    assert (await provider.get_metadata("k")).state == "archived"
    await provider.aclose()


# --- signing / verification -------------------------------------------------


async def test_sign_bytes_unwraps_the_versioned_vault_signature() -> None:
    vault = _FakeVault()
    vault.route(
        "POST", f"/v1/{_MOUNT}/sign/k", 200, {"data": {"signature": _vault_token(2, b"sig-bytes")}}
    )
    provider = _provider(vault)
    assert await provider.sign_bytes("k", Algorithm.ED25519, 2, b"payload") == b"sig-bytes"
    sent = json.loads(vault.requests[-1].content)
    assert base64.b64decode(sent["input"]) == b"payload"
    assert sent["key_version"] == 2  # version-pinned
    await provider.aclose()


async def test_verify_bytes_returns_the_daemon_verdict() -> None:
    vault = _FakeVault()
    vault.route("POST", f"/v1/{_MOUNT}/verify/k", 200, {"data": {"valid": True}})
    provider = _provider(vault)
    key_id = KeyId(ProviderKind.VAULT_TRANSIT, "k", 2)
    assert await provider.verify_bytes(b"data", b"sig", Algorithm.ED25519, key_id) is True
    await provider.aclose()


async def test_verify_bytes_is_false_for_an_unknown_key_rather_than_raising() -> None:
    vault = _FakeVault()
    vault.route("POST", f"/v1/{_MOUNT}/verify/gone", 404, {"errors": []})
    provider = _provider(vault)
    key_id = KeyId(ProviderKind.VAULT_TRANSIT, "gone", 1)
    assert await provider.verify_bytes(b"d", b"s", Algorithm.ED25519, key_id) is False
    await provider.aclose()


async def test_public_key_is_exported_for_the_pinned_version() -> None:
    vault = _FakeVault()
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 200, _key_read_body(public="PUBKEY"))
    provider = _provider(vault)
    key_id = KeyId(ProviderKind.VAULT_TRANSIT, "k", 1)
    assert await provider.public_key(key_id) == b"PUBKEY"
    await provider.aclose()


async def test_public_key_raises_when_the_version_has_none() -> None:
    vault = _FakeVault()
    vault.route("GET", f"/v1/{_MOUNT}/keys/k", 200, {"data": {"keys": {"1": {}}}})
    provider = _provider(vault)
    key_id = KeyId(ProviderKind.VAULT_TRANSIT, "k", 1)
    with pytest.raises(CapabilityNotSupported):
        await provider.public_key(key_id)
    await provider.aclose()


# --- encryption / data keys -------------------------------------------------


async def test_encrypt_returns_a_version_pinned_ciphertext() -> None:
    vault = _FakeVault()
    vault.route(
        "POST", f"/v1/{_MOUNT}/encrypt/k", 200, {"data": {"ciphertext": _vault_token(3, b"ct")}}
    )
    provider = _provider(vault)
    result = await provider.encrypt("k", Algorithm.AES_256_GCM, b"secret", None)
    assert result.key_id.version == 3
    assert base64.b64decode(json.loads(vault.requests[-1].content)["plaintext"]) == b"secret"
    await provider.aclose()


async def test_encrypt_passes_aad_as_the_transit_context() -> None:
    vault = _FakeVault()
    vault.route(
        "POST", f"/v1/{_MOUNT}/encrypt/k", 200, {"data": {"ciphertext": _vault_token(1, b"ct")}}
    )
    provider = _provider(vault)
    await provider.encrypt("k", Algorithm.AES_256_GCM, b"secret", b"aad-value")
    assert base64.b64decode(json.loads(vault.requests[-1].content)["context"]) == b"aad-value"
    await provider.aclose()


async def test_decrypt_round_trips_the_plaintext() -> None:
    vault = _FakeVault()
    vault.route(
        "POST",
        f"/v1/{_MOUNT}/decrypt/k",
        200,
        {"data": {"plaintext": base64.b64encode(b"secret").decode()}},
    )
    provider = _provider(vault)
    ciphertext = Ciphertext(
        value=_vault_token(1, b"ct").encode(),
        nonce=b"",
        algorithm=Algorithm.AES_256_GCM,
        key_id=KeyId(ProviderKind.VAULT_TRANSIT, "k", 1),
    )
    assert await provider.decrypt(ciphertext, b"aad") == b"secret"
    assert "context" in json.loads(vault.requests[-1].content)
    await provider.aclose()


async def test_generate_data_key_returns_plaintext_and_wrapped_forms() -> None:
    vault = _FakeVault()
    vault.route(
        "POST",
        f"/v1/{_MOUNT}/datakey/plaintext/k",
        200,
        {
            "data": {
                "plaintext": base64.b64encode(b"dek-material").decode(),
                "ciphertext": _vault_token(2, b"wrapped"),
            }
        },
    )
    provider = _provider(vault)
    data_key = await provider.generate_data_key("k")
    assert data_key.plaintext == b"dek-material"
    assert data_key.key_id.version == 2
    await provider.aclose()
