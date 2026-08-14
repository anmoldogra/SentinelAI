"""KeyManagementService — the port every consumer depends on (ADR-0009, revised).

The facade owns the **self-authenticating signature envelope** (C1): it builds a versioned
``SignedHeader`` (algorithm, key id+version, purpose, required-algorithm set, payload hash,
timestamp), has the provider sign the header's canonical bytes, and on verify enforces payload
binding, per-signature validity, bundle consistency, and that **every required algorithm is
present** (downgrade defense). Providers only sign/verify raw bytes. All backend failures fail
closed. ``start()`` begins provider background work (Vault lease renewal).
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter

from fastapi import Request

from sentinelai.platform.config import Settings
from sentinelai.platform.config import settings as default_settings
from sentinelai.platform.crypto.audit import AuditSink, StructlogAuditSink
from sentinelai.platform.crypto.backends.dev import DevKmsProvider
from sentinelai.platform.crypto.backends.vault import VaultTransitProvider
from sentinelai.platform.crypto.exceptions import (
    CapabilityNotSupported,
    InsecureConfiguration,
    ProviderNotAvailable,
)
from sentinelai.platform.crypto.metrics import KMS_LATENCY, KMS_OPERATIONS, KMS_ROTATIONS
from sentinelai.platform.crypto.policy import AlgorithmPolicy
from sentinelai.platform.crypto.provider import CryptoProvider
from sentinelai.platform.crypto.registry import KeyRegistry
from sentinelai.platform.crypto.resilience import RetryPolicy
from sentinelai.platform.crypto.types import (
    CANONICAL_HEADER_VERSION,
    PAYLOAD_HASH_ALGORITHM,
    SIGNATURE_VERSION,
    Algorithm,
    Ciphertext,
    DataKey,
    HealthState,
    HealthStatus,
    KeyId,
    KeyMetadata,
    KeyPurpose,
    KeyRef,
    ProviderKind,
    Signature,
    SignatureBundle,
    SignedHeader,
    payload_hash,
)

_SIGNING_PURPOSES = frozenset(
    {KeyPurpose.ROOT_TRUST, KeyPurpose.EVIDENCE_ROOT, KeyPurpose.EVENT_ROOT}
)


def _record(
    op: str, provider: ProviderKind, algo: str, purpose: str, result: str, seconds: float
) -> None:
    KMS_OPERATIONS.labels(op, str(provider), algo, purpose, result).inc()
    KMS_LATENCY.labels(op, str(provider)).observe(seconds)


class KeyManagementService:
    def __init__(self, registry: KeyRegistry, policy: AlgorithmPolicy, audit: AuditSink) -> None:
        self._registry = registry
        self._policy = policy
        self._audit = audit

    async def start(self) -> None:
        for provider in self._registry.providers():
            starter = getattr(provider, "start", None)
            if starter is not None:
                await starter()

    async def aclose(self) -> None:
        for provider in self._registry.providers():
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()

    def _algorithm_for(self, purpose: KeyPurpose) -> Algorithm:
        if purpose in _SIGNING_PURPOSES:
            return self._policy.signing_algorithms(purpose)[0]
        return self._policy.encryption_algorithm(purpose)

    # -- signing (self-authenticating envelope, PQC-ready bundle) ----------
    async def sign(self, ref: KeyRef, message: bytes) -> SignatureBundle:
        provider, key = self._registry.resolve(ref)
        caps = provider.capabilities()
        required = tuple(
            sorted(self._policy.signing_algorithms(ref.purpose), key=lambda a: a.value)
        )
        ph = payload_hash(message)
        created = datetime.now(UTC).isoformat()
        sigs: list[Signature] = []
        for algorithm in required:
            if not caps.supports_signing(algorithm):
                raise CapabilityNotSupported(f"{provider.kind} cannot sign with {algorithm}")
            version = await provider.current_version(key)
            header = SignedHeader(
                canonical_header_version=CANONICAL_HEADER_VERSION,
                signature_version=SIGNATURE_VERSION,
                algorithm=algorithm,
                key_id=KeyId(provider.kind, key, version),
                key_purpose=ref.purpose,
                required_algorithms=required,
                payload_hash=ph,
                payload_hash_algorithm=PAYLOAD_HASH_ALGORITHM,
                created_at=created,
            )
            start = perf_counter()
            try:
                value = await provider.sign_bytes(key, algorithm, version, header.canonical_bytes())
            except Exception:
                _record(
                    "sign", provider.kind, algorithm, ref.purpose, "failure", perf_counter() - start
                )
                raise
            _record(
                "sign", provider.kind, algorithm, ref.purpose, "success", perf_counter() - start
            )
            sigs.append(Signature(header=header, value=value))
        return SignatureBundle(tuple(sigs))

    async def verify(self, message: bytes, bundle: SignatureBundle) -> bool:
        expected_hash = payload_hash(message)
        required: tuple[str, ...] | None = None
        present: set[str] = set()
        for sig in bundle.signatures:
            header = sig.header
            # (1) the payload actually signed matches this message
            if (
                header.payload_hash != expected_hash
                or header.payload_hash_algorithm != PAYLOAD_HASH_ALGORITHM
            ):
                return False
            # (2) the signature over the canonical header verifies (authenticates ALL metadata)
            provider = self._registry.provider_for(header.key_id)
            start = perf_counter()
            ok = await provider.verify_bytes(
                header.canonical_bytes(), sig.value, header.algorithm, header.key_id
            )
            _record(
                "verify",
                provider.kind,
                header.algorithm,
                "n/a",
                "success" if ok else "invalid",
                perf_counter() - start,
            )
            if not ok:
                return False
            # (3) every signature in the bundle must agree on the required set
            req = tuple(sorted(a.value for a in header.required_algorithms))
            if required is None:
                required = req
            elif req != required:
                return False
            present.add(header.algorithm.value)
        # (4) downgrade defense: every required algorithm must be present
        return required is not None and set(required).issubset(present)

    # -- encryption / data keys --------------------------------------------
    async def encrypt(self, ref: KeyRef, plaintext: bytes, aad: bytes | None = None) -> Ciphertext:
        provider, key = self._registry.resolve(ref)
        algorithm = self._policy.encryption_algorithm(ref.purpose)
        if not provider.capabilities().supports_encryption(algorithm):
            raise CapabilityNotSupported(f"{provider.kind} cannot encrypt with {algorithm}")
        start = perf_counter()
        try:
            ct = await provider.encrypt(key, algorithm, plaintext, aad)
        except Exception:
            _record(
                "encrypt", provider.kind, algorithm, ref.purpose, "failure", perf_counter() - start
            )
            raise
        _record("encrypt", provider.kind, algorithm, ref.purpose, "success", perf_counter() - start)
        return ct

    async def decrypt(self, ciphertext: Ciphertext, aad: bytes | None = None) -> bytes:
        provider = self._registry.provider_for(ciphertext.key_id)
        start = perf_counter()
        pt = await provider.decrypt(ciphertext, aad)
        _record(
            "decrypt", provider.kind, ciphertext.algorithm, "n/a", "success", perf_counter() - start
        )
        return pt

    async def generate_data_key(self, ref: KeyRef) -> DataKey:
        provider, key = self._registry.resolve(ref)
        if not provider.capabilities().data_keys:
            raise CapabilityNotSupported(f"{provider.kind} cannot generate data keys")
        return await provider.generate_data_key(key)

    async def public_key(self, key_id: KeyId) -> bytes:
        return await self._registry.provider_for(key_id).public_key(key_id)

    # -- auditable lifecycle -----------------------------------------------
    async def create_key(self, ref: KeyRef) -> KeyMetadata:
        provider, key = self._registry.resolve(ref)
        meta = await provider.create_key(key, ref.purpose, self._algorithm_for(ref.purpose))
        await self._audit.record(
            operation="create", provider=provider.kind, purpose=ref.purpose, key_id=meta.key_id
        )
        return meta

    async def rotate(self, ref: KeyRef) -> KeyId:
        provider, key = self._registry.resolve(ref)
        key_id = await provider.rotate(key)
        KMS_ROTATIONS.labels(str(provider.kind), str(ref.purpose)).inc()
        await self._audit.record(
            operation="rotate", provider=provider.kind, purpose=ref.purpose, key_id=key_id
        )
        return key_id

    async def _lifecycle(self, ref: KeyRef, op: str) -> None:
        provider, key = self._registry.resolve(ref)
        await getattr(provider, op)(key)
        await self._audit.record(operation=op, provider=provider.kind, purpose=ref.purpose)

    async def disable(self, ref: KeyRef) -> None:
        await self._lifecycle(ref, "disable")

    async def enable(self, ref: KeyRef) -> None:
        await self._lifecycle(ref, "enable")

    async def archive(self, ref: KeyRef) -> None:
        await self._lifecycle(ref, "archive")

    async def destroy(self, ref: KeyRef) -> None:
        await self._lifecycle(ref, "destroy")

    async def list_versions(self, ref: KeyRef) -> tuple[int, ...]:
        provider, key = self._registry.resolve(ref)
        return await provider.list_versions(key)

    async def get_metadata(self, ref: KeyRef) -> KeyMetadata:
        provider, key = self._registry.resolve(ref)
        return await provider.get_metadata(key)

    async def health(self) -> HealthStatus:
        worst = HealthStatus(HealthState.READY, ProviderKind.DEV)
        for provider in self._registry.providers():
            status = await provider.health()
            if status.state == HealthState.UNAVAILABLE:
                return status
            if status.state == HealthState.DEGRADED:
                worst = status
        return worst


# --- construction -----------------------------------------------------------
def build_provider(cfg: Settings) -> CryptoProvider:
    kind = ProviderKind(cfg.kms_provider)
    if kind is ProviderKind.DEV:
        return DevKmsProvider(cfg.kms_dev_keystore, is_production=cfg.is_production)
    if kind is ProviderKind.VAULT_TRANSIT:
        if cfg.is_production:
            # Fail closed on a placeholder/default credential in production (M6): a national
            # evidence platform must never sign under a well-known token.
            if cfg.vault_auth_method == "token":
                tok = cfg.vault_token.get_secret_value()
                if tok in ("", "dev-only-change-me", "root", "dev-only-token"):
                    raise InsecureConfiguration(
                        "VAULT_TOKEN is empty or a default/placeholder value in production"
                    )
            elif cfg.vault_auth_method == "approle":
                if not cfg.vault_role_id or not cfg.vault_secret_id:
                    raise InsecureConfiguration(
                        "AppRole auth requires VAULT_ROLE_ID + VAULT_SECRET_ID in production"
                    )
            else:
                raise InsecureConfiguration(
                    f"unsupported VAULT_AUTH_METHOD '{cfg.vault_auth_method}'"
                )
        return VaultTransitProvider(
            addr=cfg.vault_addr,
            mount=cfg.vault_transit_mount,
            token=cfg.vault_token.get_secret_value(),
            namespace=cfg.vault_namespace,
            auth_method=cfg.vault_auth_method,
            role_id=cfg.vault_role_id,
            secret_id=cfg.vault_secret_id.get_secret_value() if cfg.vault_secret_id else None,
            retry=RetryPolicy(
                max_attempts=cfg.kms_retry_max_attempts,
                base_delay=cfg.kms_retry_base_delay,
                max_delay=cfg.kms_retry_max_delay,
                timeout=cfg.kms_timeout_seconds,
            ),
            breaker_failure_threshold=cfg.kms_breaker_failure_threshold,
            breaker_reset_seconds=cfg.kms_breaker_reset_seconds,
        )
    raise ProviderNotAvailable(
        f"KMS provider '{kind}' is a registered slot but not yet implemented — "
        "add its provider behind CryptoProvider before selecting it"
    )


def create_kms(cfg: Settings | None = None) -> KeyManagementService:
    cfg = cfg or default_settings
    registry = KeyRegistry(build_provider(cfg))
    policy = AlgorithmPolicy.from_config(
        signing_algorithm=cfg.kms_signing_algorithm, hybrid=cfg.kms_hybrid_signing
    )
    return KeyManagementService(registry, policy, StructlogAuditSink())


def get_kms(request: Request) -> KeyManagementService:
    kms: KeyManagementService | None = getattr(request.app.state, "kms", None)
    if kms is None:  # pragma: no cover
        raise RuntimeError("KMS not initialized on app.state")
    return kms
