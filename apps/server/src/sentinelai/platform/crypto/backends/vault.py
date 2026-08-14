"""Vault Transit KMS provider — ADR-0009 (revised: H1 lease renewal + H2 resilience).

Real async client for Vault's Transit engine (keys never leave Vault). Supports token and
AppRole auth with automatic lease renewal / re-authentication (fails closed on renewal loss),
and wraps every call in bounded jittered-backoff retry behind a circuit breaker. Signing is the
raw-bytes surface; the facade owns the SignedHeader. Requires a running Vault — exercised by the
contract test, not the in-repo static checks.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import UTC, datetime
from typing import Any

import httpx

from sentinelai.platform.crypto.exceptions import (
    CapabilityNotSupported,
    CryptoError,
    KeyNotFound,
    KmsUnavailable,
)
from sentinelai.platform.crypto.metrics import KMS_LEASE_RENEWALS
from sentinelai.platform.crypto.resilience import CircuitBreaker, RetryPolicy, call_resilient
from sentinelai.platform.crypto.types import (
    Algorithm,
    Ciphertext,
    DataKey,
    HealthState,
    HealthStatus,
    KeyId,
    KeyMetadata,
    KeyPurpose,
    ProviderCapabilities,
    ProviderKind,
)
from sentinelai.platform.logging import log

_VAULT_KEY_TYPE = {
    Algorithm.ED25519: "ed25519",
    Algorithm.ECDSA_P256: "ecdsa-p256",
    Algorithm.AES_256_GCM: "aes256-gcm96",
}


def _parse_versioned(token: str) -> tuple[int, bytes]:
    _, ver, payload = token.split(":", 2)
    return int(ver.lstrip("v")), base64.b64decode(payload)


class VaultTransitProvider:
    kind = ProviderKind.VAULT_TRANSIT

    def __init__(
        self,
        *,
        addr: str,
        mount: str,
        token: str,
        namespace: str | None = None,
        auth_method: str = "token",
        role_id: str | None = None,
        secret_id: str | None = None,
        retry: RetryPolicy | None = None,
        breaker_failure_threshold: int = 5,
        breaker_reset_seconds: float = 30.0,
    ) -> None:
        self._mount = mount
        self._namespace = namespace
        self._auth_method = auth_method
        self._role_id = role_id
        self._secret_id = secret_id
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=addr.rstrip("/"), timeout=(retry or RetryPolicy()).timeout
        )
        self._retry = retry or RetryPolicy()
        self._breaker = CircuitBreaker(
            provider=self.kind.value,
            failure_threshold=breaker_failure_threshold,
            reset_seconds=breaker_reset_seconds,
        )
        self._lease_seconds = 0
        self._renewable = False
        self._healthy = True
        self._renew_task: asyncio.Task[None] | None = None

    # -- lifecycle: auth + lease renewal (H1) ------------------------------
    async def start(self) -> None:
        if self._auth_method == "approle":
            await self._login_approle()
        else:
            await self._lookup_self()  # discover renewability/TTL of the provided token
        if self._renewable and self._renew_task is None:
            self._renew_task = asyncio.create_task(self._renewal_loop())

    async def aclose(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._renew_task
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        h = {"X-Vault-Token": self._token}
        if self._namespace:
            h["X-Vault-Namespace"] = self._namespace
        return h

    async def _login_approle(self) -> None:
        if not self._role_id or not self._secret_id:
            raise KmsUnavailable("approle auth requires role_id + secret_id")
        try:
            r = await self._client.post(
                "/v1/auth/approle/login",
                json={"role_id": self._role_id, "secret_id": self._secret_id},
                headers={"X-Vault-Namespace": self._namespace} if self._namespace else None,
            )
        except httpx.HTTPError as exc:
            raise KmsUnavailable(f"vault approle login unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise KmsUnavailable(f"vault approle login failed: {r.status_code}")
        auth = r.json()["auth"]
        self._token = auth["client_token"]
        self._lease_seconds = int(auth.get("lease_duration", 0))
        self._renewable = bool(auth.get("renewable", False))
        self._healthy = True
        KMS_LEASE_RENEWALS.labels(self.kind.value, "login").inc()

    async def _lookup_self(self) -> None:
        try:
            r = await self._client.get("/v1/auth/token/lookup-self", headers=self._headers())
            if r.status_code < 400:
                data = r.json()["data"]
                self._lease_seconds = int(data.get("ttl", 0))
                self._renewable = bool(data.get("renewable", False))
        except httpx.HTTPError:  # non-fatal; token may still work
            self._renewable = False

    async def _renewal_loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, self._lease_seconds * 0.6))
            try:
                r = await self._client.post("/v1/auth/token/renew-self", headers=self._headers())
                if r.status_code >= 400:
                    raise KmsUnavailable(f"renew-self {r.status_code}")
                auth = r.json()["auth"]
                self._lease_seconds = int(auth.get("lease_duration", self._lease_seconds))
                self._healthy = True
                KMS_LEASE_RENEWALS.labels(self.kind.value, "renew").inc()
            except (httpx.HTTPError, KmsUnavailable, KeyError) as exc:
                KMS_LEASE_RENEWALS.labels(self.kind.value, "failure").inc()
                log.warning("vault_lease_renewal_failed", detail=str(exc))
                if self._auth_method == "approle":
                    try:
                        await self._login_approle()  # graceful re-authentication
                        continue
                    except KmsUnavailable:
                        pass
                self._healthy = False  # fail closed — health goes UNAVAILABLE

    # -- capabilities / health ---------------------------------------------
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            kind=self.kind,
            signing=True,
            encryption=True,
            data_keys=True,
            rotation=True,
            attestation=False,
            export_public_key=True,
            algorithms=frozenset({Algorithm.ED25519, Algorithm.ECDSA_P256, Algorithm.AES_256_GCM}),
        )

    async def health(self) -> HealthStatus:
        if not self._healthy:
            return HealthStatus(HealthState.UNAVAILABLE, self.kind, detail="auth lease lost")
        try:
            r = await self._client.get("/v1/sys/health")
        except httpx.HTTPError as exc:
            return HealthStatus(HealthState.UNAVAILABLE, self.kind, detail=str(exc))
        if r.status_code == 200:
            return HealthStatus(HealthState.READY, self.kind)
        if r.status_code in (429, 472, 473):
            return HealthStatus(HealthState.DEGRADED, self.kind, detail=f"status {r.status_code}")
        return HealthStatus(HealthState.UNAVAILABLE, self.kind, detail=f"status {r.status_code}")

    # -- resilient request core --------------------------------------------
    async def _raw(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            r = await self._client.request(
                method, f"/v1/{path}", json=payload, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise KmsUnavailable(f"vault unreachable: {exc}") from exc
        if r.status_code == 404:
            raise KeyNotFound(path)  # deterministic — must NOT be retried
        if r.status_code == 429 or r.status_code >= 500:
            raise KmsUnavailable(f"vault transient error {r.status_code}")  # retryable
        if r.status_code >= 400:
            raise CryptoError(f"vault request failed: {r.status_code}")  # deterministic
        return r.json().get("data", {}) if r.content else {}

    async def _api(
        self, method: str, path: str, payload: dict[str, Any] | None = None, *, op: str
    ) -> dict[str, Any]:
        return await call_resilient(
            lambda: self._raw(method, path, payload),
            provider=self.kind.value,
            operation=op,
            policy=self._retry,
            breaker=self._breaker,
        )

    # -- lifecycle ----------------------------------------------------------
    async def create_key(self, key: str, purpose: KeyPurpose, algorithm: Algorithm) -> KeyMetadata:
        if algorithm not in _VAULT_KEY_TYPE:
            raise CapabilityNotSupported(f"vault transit cannot create '{algorithm}'")
        await self._api(
            "POST", f"{self._mount}/keys/{key}", {"type": _VAULT_KEY_TYPE[algorithm]}, op="create"
        )
        return await self.get_metadata(key)

    async def rotate(self, key: str) -> KeyId:
        await self._api("POST", f"{self._mount}/keys/{key}/rotate", {}, op="rotate")
        return (await self.get_metadata(key)).key_id

    async def enable(self, key: str) -> None:
        await self._api(
            "POST", f"{self._mount}/keys/{key}/config", {"min_decryption_version": 1}, op="enable"
        )

    async def disable(self, key: str) -> None:
        await self._api(
            "POST", f"{self._mount}/keys/{key}/config", {"min_encryption_version": 0}, op="disable"
        )

    async def archive(self, key: str) -> None:
        await self._api(
            "POST", f"{self._mount}/keys/{key}/config", {"deletion_allowed": False}, op="archive"
        )

    async def destroy(self, key: str) -> None:
        await self._api(
            "POST", f"{self._mount}/keys/{key}/config", {"deletion_allowed": True}, op="destroy"
        )
        await self._api("DELETE", f"{self._mount}/keys/{key}", None, op="destroy")

    async def list_versions(self, key: str) -> tuple[int, ...]:
        data = await self._api("GET", f"{self._mount}/keys/{key}", None, op="read")
        return tuple(sorted(int(v) for v in data.get("keys", {})))

    async def get_metadata(self, key: str) -> KeyMetadata:
        data = await self._api("GET", f"{self._mount}/keys/{key}", None, op="read")
        latest = int(data.get("latest_version", 1))
        algo = next(
            (a for a, t in _VAULT_KEY_TYPE.items() if t == data.get("type")), Algorithm.ED25519
        )
        return KeyMetadata(
            key_id=KeyId(self.kind, key, latest),
            purpose=KeyPurpose[data.get("_purpose", "ROOT_TRUST").upper()]
            if data.get("_purpose")
            else KeyPurpose.ROOT_TRUST,
            algorithm=algo,
            state="enabled" if not data.get("deletion_allowed") else "archived",
            created_at=datetime.now(UTC),
            versions=tuple(sorted(int(v) for v in data.get("keys", {}))),
        )

    async def current_version(self, key: str) -> int:
        return (await self.get_metadata(key)).key_id.version

    # -- signing (raw bytes, version-pinned) -------------------------------
    async def sign_bytes(self, key: str, algorithm: Algorithm, version: int, data: bytes) -> bytes:
        out = await self._api(
            "POST",
            f"{self._mount}/sign/{key}",
            {"input": base64.b64encode(data).decode(), "key_version": version},
            op="sign",
        )
        _, raw = _parse_versioned(out["signature"])
        return raw

    async def verify_bytes(
        self, data: bytes, signature: bytes, algorithm: Algorithm, key_id: KeyId
    ) -> bool:
        vault_sig = f"vault:v{key_id.version}:{base64.b64encode(signature).decode()}"
        try:
            out = await self._api(
                "POST",
                f"{self._mount}/verify/{key_id.backend_ref}",
                {"input": base64.b64encode(data).decode(), "signature": vault_sig},
                op="verify",
            )
        except (KeyNotFound, CryptoError):
            return False
        return bool(out.get("valid", False))

    async def public_key(self, key_id: KeyId) -> bytes:
        data = await self._api("GET", f"{self._mount}/keys/{key_id.backend_ref}", None, op="read")
        entry = data.get("keys", {}).get(str(key_id.version))
        pub = entry.get("public_key") if isinstance(entry, dict) else None
        if not isinstance(pub, str) or not pub:
            raise CapabilityNotSupported("no exportable public key for this key/version")
        return pub.encode()

    # -- encryption / data keys --------------------------------------------
    async def encrypt(
        self, key: str, algorithm: Algorithm, plaintext: bytes, aad: bytes | None
    ) -> Ciphertext:
        payload: dict[str, Any] = {"plaintext": base64.b64encode(plaintext).decode()}
        if aad is not None:
            payload["context"] = base64.b64encode(aad).decode()
        out = await self._api("POST", f"{self._mount}/encrypt/{key}", payload, op="encrypt")
        version, _ = _parse_versioned(out["ciphertext"])
        return Ciphertext(
            value=out["ciphertext"].encode(),
            nonce=b"",
            algorithm=algorithm,
            key_id=KeyId(self.kind, key, version),
        )

    async def decrypt(self, ciphertext: Ciphertext, aad: bytes | None) -> bytes:
        payload: dict[str, Any] = {"ciphertext": ciphertext.value.decode()}
        if aad is not None:
            payload["context"] = base64.b64encode(aad).decode()
        out = await self._api(
            "POST", f"{self._mount}/decrypt/{ciphertext.key_id.backend_ref}", payload, op="decrypt"
        )
        return base64.b64decode(out["plaintext"])

    async def generate_data_key(self, key: str) -> DataKey:
        out = await self._api("POST", f"{self._mount}/datakey/plaintext/{key}", {}, op="datakey")
        version, _ = _parse_versioned(out["ciphertext"])
        return DataKey(
            plaintext=base64.b64decode(out["plaintext"]),
            wrapped=out["ciphertext"].encode(),
            key_id=KeyId(self.kind, key, version),
        )
