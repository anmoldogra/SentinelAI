"""SentinelAI — platform.crypto package (ADR-0009).

The single, provider-agnostic cryptographic trust boundary. Consumers import ONLY from here
(the `KeyManagementService` facade + value objects); provider selection is entirely by config.
"""

from __future__ import annotations

from sentinelai.platform.crypto.exceptions import (
    AlgorithmNotAllowed,
    CapabilityNotSupported,
    CryptoError,
    InsecureConfiguration,
    KeyNotFound,
    KmsUnavailable,
    ProviderNotAvailable,
    SignatureInvalid,
)
from sentinelai.platform.crypto.kms import (
    KeyManagementService,
    create_kms,
    get_kms,
)
from sentinelai.platform.crypto.types import (
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
)

__all__ = [
    "Algorithm",
    "AlgorithmNotAllowed",
    "CapabilityNotSupported",
    "Ciphertext",
    "CryptoError",
    "DataKey",
    "HealthState",
    "HealthStatus",
    "InsecureConfiguration",
    "KeyId",
    "KeyManagementService",
    "KeyMetadata",
    "KeyNotFound",
    "KeyPurpose",
    "KeyRef",
    "KmsUnavailable",
    "ProviderKind",
    "ProviderNotAvailable",
    "Signature",
    "SignatureBundle",
    "SignatureInvalid",
    "create_kms",
    "get_kms",
]
