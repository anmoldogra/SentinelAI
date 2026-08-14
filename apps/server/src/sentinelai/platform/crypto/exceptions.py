"""KMS error taxonomy — ADR-0009.

Infrastructure errors (not domain errors). They never carry key material or plaintext.
Callers that surface them over HTTP translate to 503 (unavailable) or 500 — a signing
subsystem that cannot reach its keys must fail closed, never proceed unsigned.
"""

from __future__ import annotations


class CryptoError(Exception):
    """Base for all KMS/crypto errors."""


class KmsUnavailable(CryptoError):
    """The provider backend (Vault/HSM/cloud KMS) is unreachable — fail closed."""


class KeyNotFound(CryptoError):
    """The requested logical/backend key or version does not exist."""


class SignatureInvalid(CryptoError):
    """A signature failed verification where a valid one was required."""


class AlgorithmNotAllowed(CryptoError):
    """An algorithm outside the configured policy allow-list was requested."""


class CapabilityNotSupported(CryptoError):
    """The active provider does not advertise the requested capability/algorithm."""


class ProviderNotAvailable(CryptoError):
    """A registered provider slot was selected but is not implemented/installed."""


class InsecureConfiguration(CryptoError):
    """A configuration that is unsafe for the current environment (e.g. the dev provider
    selected while ``APP_ENV=production``). Raised at startup — fail closed."""


class KeystoreCorrupt(CryptoError):
    """A keystore manifest failed its integrity checksum — possible corruption/tampering."""
