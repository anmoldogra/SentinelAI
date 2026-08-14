"""Crypto value objects — ADR-0009.

These types carry the **cryptographic-agility metadata that must survive forever**
(algorithm, provider, key id, key version, signature version, creation timestamp), so a
signature produced today remains verifiable after key rotation, algorithm migration, or a
provider swap over the platform's 10-20 year life. Secret-bearing types redact their repr
(security invariant #11 — no key material in logs/tracebacks/serialization).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# Envelope version for the signed metadata; bump ONLY on a breaking change, and it is
# carried on (and covered by) every signature so old signatures stay verifiable forever.
SIGNATURE_VERSION = 1
# Canonical header encoding version — lets the encoding itself evolve without orphaning
# historical evidence (the verifier dispatches on this).
CANONICAL_HEADER_VERSION = 1
PAYLOAD_HASH_ALGORITHM = "SHA-256"


def payload_hash(message: bytes) -> str:
    return hashlib.sha256(message).hexdigest()


class Algorithm(StrEnum):
    """Named algorithm targets. Presence here is not a promise of support — a provider
    advertises which it supports via ``ProviderCapabilities`` (capability negotiation)."""

    ED25519 = "ED25519"
    ECDSA_P256 = "ECDSA_P256"
    RSA_PSS_2048 = "RSA_PSS_2048"
    ML_DSA_65 = "ML_DSA_65"  # post-quantum (FIPS 204) — reserved; no software provider yet
    AES_256_GCM = "AES_256_GCM"


SIGNING_ALGORITHMS = frozenset(
    {Algorithm.ED25519, Algorithm.ECDSA_P256, Algorithm.RSA_PSS_2048, Algorithm.ML_DSA_65}
)
ENCRYPTION_ALGORITHMS = frozenset({Algorithm.AES_256_GCM})


class KeyPurpose(StrEnum):
    """The key hierarchy (ADR-0009 §7): a root trust key anchors the four functional roots,
    under which derived (data) keys live via envelope encryption."""

    ROOT_TRUST = "root_trust"
    EVIDENCE_ROOT = "evidence_root"  # ADR-0003 custody/audit signing + Merkle roots
    EVENT_ROOT = "event_root"  # ADR-0007 outbox signing
    STORAGE_ROOT = "storage_root"  # ADR-0008 object/column envelope encryption
    SESSION_ROOT = "session_root"  # ADR-0010 token/secret keying


class ProviderKind(StrEnum):
    DEV = "dev"
    VAULT_TRANSIT = "vault_transit"
    AWS_KMS = "aws_kms"
    AZURE_KEY_VAULT = "azure_key_vault"
    GCP_KMS = "gcp_kms"
    PKCS11 = "pkcs11"


class HealthState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class KeyRef:
    """Logical key reference used by callers — never a backend id, never an algorithm."""

    purpose: KeyPurpose
    name: str = "default"


@dataclass(frozen=True, slots=True)
class KeyId:
    """Resolved, version-pinned key identity carried on every signature/ciphertext."""

    provider: ProviderKind
    backend_ref: str
    version: int


@dataclass(frozen=True, slots=True)
class SignedHeader:
    """The bytes that are ACTUALLY signed (ADR-0009 §C1). Everything integrity-relevant is
    inside the signature: version, algorithm, key id + version, purpose, the required-algorithm
    set (downgrade defense), the payload hash, and the creation timestamp. ``canonical_bytes``
    is a deterministic RFC-8785-style encoding independent of any JSONB round-trip."""

    canonical_header_version: int
    signature_version: int
    algorithm: Algorithm
    key_id: KeyId
    key_purpose: KeyPurpose
    required_algorithms: tuple[Algorithm, ...]
    payload_hash: str
    payload_hash_algorithm: str
    created_at: str  # ISO-8601; authenticated because it is inside the signed header

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "chv": self.canonical_header_version,
                "sv": self.signature_version,
                "alg": self.algorithm.value,
                "kid": {
                    "p": self.key_id.provider.value,
                    "r": self.key_id.backend_ref,
                    "v": self.key_id.version,
                },
                "kp": self.key_purpose.value,
                "req": sorted(a.value for a in self.required_algorithms),
                "ph": self.payload_hash,
                "pha": self.payload_hash_algorithm,
                "ts": self.created_at,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


@dataclass(frozen=True, slots=True)
class Signature:
    """A signature over a ``SignedHeader``. All agility metadata lives in — and is protected
    by — the header, so tampering with any of it invalidates the signature."""

    header: SignedHeader
    value: bytes

    @property
    def algorithm(self) -> Algorithm:
        return self.header.algorithm

    @property
    def key_id(self) -> KeyId:
        return self.header.key_id

    @property
    def signature_version(self) -> int:
        return self.header.signature_version

    @property
    def created_at(self) -> str:
        return self.header.created_at

    def b64(self) -> str:
        return base64.b64encode(self.value).decode()


@dataclass(frozen=True, slots=True)
class SignatureBundle:
    """One-or-more signatures (PQC-hybrid ready). Each signature independently binds the same
    ``required_algorithms`` set, so stripping a signature to downgrade is detectable."""

    signatures: tuple[Signature, ...]

    def __post_init__(self) -> None:
        if not self.signatures:
            raise ValueError("a signature bundle must contain at least one signature")

    @property
    def primary(self) -> Signature:
        return self.signatures[0]


@dataclass(frozen=True, slots=True)
class Ciphertext:
    """Encrypted output. Not secret — safe to persist and to repr."""

    value: bytes
    nonce: bytes
    algorithm: Algorithm
    key_id: KeyId


@dataclass(frozen=True)
class DataKey:
    """Envelope-encryption data key. ``plaintext`` is SECRET: use immediately, never persist,
    never log. ``wrapped`` is safe to store. repr is redacted."""

    plaintext: bytes
    wrapped: bytes
    key_id: KeyId

    def __repr__(self) -> str:  # never leak the plaintext DEK
        return (
            f"DataKey(key_id={self.key_id!r}, plaintext=<REDACTED>, wrapped=<{len(self.wrapped)}B>)"
        )


@dataclass(frozen=True, slots=True)
class KeyMetadata:
    key_id: KeyId
    purpose: KeyPurpose
    algorithm: Algorithm
    state: str
    created_at: datetime
    versions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider can do — checked by the facade before every dispatch."""

    kind: ProviderKind
    signing: bool
    encryption: bool
    data_keys: bool
    rotation: bool
    attestation: bool
    export_public_key: bool
    algorithms: frozenset[Algorithm]

    def supports_signing(self, algorithm: Algorithm) -> bool:
        return self.signing and algorithm in self.algorithms

    def supports_encryption(self, algorithm: Algorithm) -> bool:
        return self.encryption and algorithm in self.algorithms


@dataclass(frozen=True, slots=True)
class HealthStatus:
    state: HealthState
    provider: ProviderKind
    detail: str | None = None
