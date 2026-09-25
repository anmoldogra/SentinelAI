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

# ---------------------------------------------------------------------------------------
# Ledger verification — ADR-0003 §6 (Wave 1.4)
#
# These are the alarm. ADR-0003 §6(b) requires the scheduled re-verification job to "alarm on any
# break", and in this platform an alarm is a metric an Alertmanager rule fires on plus a CRITICAL
# log line (`deployment-architecture.md` commits to the Prometheus/Grafana/Loki stack). It is
# deliberately NOT a notification-module message: a ledger integrity failure is addressed to
# security operations, and every notification dispatch path in this codebase requires an explicit
# recipient_user_id that no part of the domain can supply for it. A notification that resolved zero
# recipients would look like alerting while reaching nobody, which is worse than no alert at all.
#
# LEDGER_VERIFICATION_STATE is a Gauge rather than a Counter on purpose: alerting wants "is this
# ledger broken right now", and a counter of historical failures cannot answer that after a restore.
# ---------------------------------------------------------------------------------------
LEDGER_VERIFICATIONS = Counter(
    "sentinelai_ledger_verifications_total",
    "Ledger verification runs by ledger and resulting state (verified/partial/failed).",
    ["ledger", "state"],
)
LEDGER_VERIFICATION_STATE = Gauge(
    "sentinelai_ledger_verification_state",
    "Last verification verdict per ledger: 0=verified, 1=partial, 2=failed.",
    ["ledger"],
)
LEDGER_VERIFICATION_FINDINGS = Counter(
    "sentinelai_ledger_verification_findings_total",
    "Individual verification findings by ledger and finding type.",
    ["ledger", "finding"],
)
LEDGER_VERIFICATION_DURATION = Histogram(
    "sentinelai_ledger_verification_seconds",
    "Wall-clock duration of one ledger verification run.",
    ["ledger"],
)
LEDGER_ANCHORS_CUT = Counter(
    "sentinelai_ledger_anchors_cut_total",
    "Anchors published to WORM storage, by ledger.",
    ["ledger"],
)
LEDGER_ANCHORED_ENTRIES = Counter(
    "sentinelai_ledger_anchored_entries_total",
    "Ledger entries newly committed to an anchor, by ledger.",
    ["ledger"],
)
LEDGER_ANCHOR_BATCH_DURATION = Histogram(
    "sentinelai_ledger_anchor_batch_seconds",
    "Wall-clock duration of cutting one anchor batch.",
    ["ledger"],
)
LEDGER_UNANCHORED_ENTRIES = Gauge(
    "sentinelai_ledger_unanchored_entries",
    "Entries not covered by any anchor — a steadily rising value means batch cutting has stopped.",
    ["ledger"],
)
