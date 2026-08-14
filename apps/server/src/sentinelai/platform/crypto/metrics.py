"""KMS observability metrics — ADR-0009 §9.

Prometheus counters/histograms for every crypto operation: latency, provider, algorithm,
key purpose, success/failure, rotation/retry counts. Labels never contain key material,
plaintext, or ciphertext.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

KMS_OPERATIONS = Counter(
    "sentinelai_kms_operations_total",
    "KMS operations by type/provider/algorithm/purpose/result.",
    ["operation", "provider", "algorithm", "purpose", "result"],
)
KMS_LATENCY = Histogram(
    "sentinelai_kms_operation_seconds",
    "KMS operation latency in seconds.",
    ["operation", "provider"],
)
KMS_ROTATIONS = Counter(
    "sentinelai_kms_rotations_total",
    "Key rotations performed.",
    ["provider", "purpose"],
)
KMS_RETRIES = Counter(
    "sentinelai_kms_retries_total",
    "Retries against a KMS provider.",
    ["provider", "operation"],
)
KMS_CIRCUIT_STATE = Gauge(
    "sentinelai_kms_circuit_state",
    "Circuit-breaker state per provider: 0=closed, 1=half_open, 2=open.",
    ["provider"],
)
KMS_LEASE_RENEWALS = Counter(
    "sentinelai_kms_lease_renewals_total",
    "Auth-token/lease renewals by result.",
    ["provider", "result"],
)
