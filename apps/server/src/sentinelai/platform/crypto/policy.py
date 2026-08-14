"""Algorithm Policy Engine — ADR-0009 §3.

Callers never name an algorithm. The policy resolves ``KeyPurpose → algorithm(s)`` from
configuration. ``Ed25519`` is today's default, not the platform algorithm; migrating to
ECDSA-P256 or a PQC scheme (ML-DSA) is a config/policy change, not a code change. Hybrid
signing returns a multi-algorithm bundle (classical + PQC) once a PQC provider is registered.
"""

from __future__ import annotations

from dataclasses import dataclass

from sentinelai.platform.crypto.exceptions import AlgorithmNotAllowed
from sentinelai.platform.crypto.types import (
    ENCRYPTION_ALGORITHMS,
    SIGNING_ALGORITHMS,
    Algorithm,
    KeyPurpose,
)


@dataclass(frozen=True, slots=True)
class AlgorithmPolicy:
    signing_bundle: tuple[Algorithm, ...]
    encryption_default: Algorithm

    @classmethod
    def from_config(cls, *, signing_algorithm: str, hybrid: bool) -> AlgorithmPolicy:
        try:
            default = Algorithm(signing_algorithm.upper())
        except ValueError as exc:
            raise AlgorithmNotAllowed(f"unknown signing algorithm '{signing_algorithm}'") from exc
        if default not in SIGNING_ALGORITHMS:
            raise AlgorithmNotAllowed(f"'{default}' is not a signing algorithm")
        # Hybrid extends the bundle with a PQC scheme when a provider supports it; today the
        # bundle is [default] and grows without any caller/API change (ADR-0009 §5).
        bundle: tuple[Algorithm, ...] = (default,)
        return cls(signing_bundle=bundle, encryption_default=Algorithm.AES_256_GCM)

    def signing_algorithms(self, purpose: KeyPurpose) -> tuple[Algorithm, ...]:
        return self.signing_bundle

    def encryption_algorithm(self, purpose: KeyPurpose) -> Algorithm:
        if self.encryption_default not in ENCRYPTION_ALGORITHMS:
            raise AlgorithmNotAllowed(f"'{self.encryption_default}' is not an encryption algorithm")
        return self.encryption_default
