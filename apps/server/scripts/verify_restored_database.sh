#!/usr/bin/env bash
#
# Automated DR / restore verification — deployment-architecture.md Part 14, ADR-0003 §3/§6.
#
# Proves that the database this container is pointed at still contains everything the Merkle roots
# published to the WORM anchor bucket committed to. Intended as the command of the
# `backup-restore-drill` CronJob in Part 14, run against a restored instance or the DR site.
#
# **It verifies; it does not restore.** The restore itself is CloudNativePG's — a `Cluster` with a
# `bootstrap.recovery` stanza, reconciled by ArgoCD from Git (deployment-architecture.md Mandatory
# Rule 1: no imperative changes against a real environment, even for a drill). This script is the
# half that answers "and is what came back intact?", which is the half nothing automated before.
#
# Required environment:
#   DATABASE_URL              the restored/DR database, read-only credentials are sufficient
#   STORAGE_ANCHOR_BUCKET     the WORM anchor bucket (read access)
#   plus the KMS settings the deployment already uses — signature verification needs *verify*
#   access to the evidence key. It never uses a private key, but it cannot be skipped: a hash
#   proves nothing about authorship, so an attestation without signatures would report a
#   rewritten ledger as intact.
#
# Exit codes are passed through from the attestation tool and are the CronJob's alarm:
#   0  verified   — the restored database matches every published anchor
#   1  could not check (database unreachable, KMS down, bucket denied) — an operational failure,
#      deliberately NOT the same signal as tampering
#   2  FAILED     — committed entries are missing or altered. Part 14: an evidentiary incident
#      (security-architecture.md §48), not a restore defect to be tidied away
#
# `--strict` is deliberately not passed. A `partial` verdict means "present but not provable" —
# pre-Wave-1.2 rows carry no signature and never can, so strict mode would make this CronJob fail
# every night on history no remedy can repair, and a permanently-red check is a check nobody reads.
# Set ATTEST_STRICT=1 on a deployment whose ledgers are entirely post-Wave-1.2.

set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must point at the database to verify}"
: "${STORAGE_ANCHOR_BUCKET:?STORAGE_ANCHOR_BUCKET must name the WORM anchor bucket}"

PYTHON="${PYTHON:-python}"
ARGS=(-m sentinelai.cli.attest verify --json)
if [[ "${ATTEST_STRICT:-0}" == "1" ]]; then
  ARGS+=(--strict)
fi
if [[ -n "${ATTEST_LEDGER:-}" ]]; then
  ARGS+=(--ledger "${ATTEST_LEDGER}")
fi

echo "dr-verification: attesting $(echo "${DATABASE_URL}" | sed 's#://[^@]*@#://***@#') against ${STORAGE_ANCHOR_BUCKET}" >&2

set +e
"${PYTHON}" "${ARGS[@]}"
STATUS=$?
set -e

case "${STATUS}" in
  0) echo "dr-verification: VERIFIED — the restored database matches every published anchor" >&2 ;;
  2) echo "dr-verification: FAILED — committed evidence is missing or altered. Treat as an evidentiary incident (security-architecture.md §48), not a restore defect." >&2 ;;
  *) echo "dr-verification: COULD NOT CHECK (exit ${STATUS}) — this is an operational failure, not a tampering verdict" >&2 ;;
esac

exit "${STATUS}"
