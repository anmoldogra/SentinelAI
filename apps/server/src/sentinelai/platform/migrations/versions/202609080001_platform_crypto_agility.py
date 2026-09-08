"""platform schema — cryptographic agility columns on audit_log (ADR-0003 §5, Wave 1.1).

Adds the six columns ADR-0003 §5 names on both evidentiary ledgers, so the integrity format can
evolve across the platform's 10-15 year life without orphaning history: ``hash_algo``,
``sig_alg``, ``key_id``, ``preimage_version``, ``signature``, ``anchor_ref``. The matching
migration for ``ingestion.evidence_custody_events`` lives in that module's own history
(``202609080002_ingestion_agility``) — no migration reaches across a schema boundary
(database-design.md §5, §11).

**All six are nullable, deliberately.** Wave 1.1 only creates the columns; Wave 1.2 populates
``hash_algo``/``sig_alg``/``key_id``/``preimage_version``/``signature`` at write time, and Wave
1.3 fills ``anchor_ref`` asynchronously once a Merkle root is timestamped. A ``NULL`` therefore
carries real meaning — "written before that wave" — which the Verification Engine (Wave 1.4)
dispatches on. Making them NOT NULL with defaults would fabricate a claim that an entry was
signed under an algorithm when no signature exists, which is worse than absent metadata on a
court-facing record.

**No backfill.** There is no evidentiary data (Alpha, never executed), which is exactly why
ADR-0003 insists this land before any real evidence is written.

**No privilege change is required.** ``202607300001_platform_privs`` grants
``INSERT, SELECT`` at *table* level, not column level, so new columns are covered by the existing
grant. The append-only trigger from ``202607280001_platform_append`` blocks UPDATE/DELETE on rows
and is unaffected by DDL that adds a column.

Revision ID: 202609080001_platform_agility
Revises: 202608300001_platform_mfa
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "202609080001_platform_agility"
down_revision = "202608300001_platform_mfa"
branch_labels = None
depends_on = None

_SCHEMA = "platform"
_TABLE = "audit_log"

# (name, type) — mirrored verbatim by the ingestion migration so one Verification Engine can
# read both ledgers through one column set.
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[Any]], ...] = (
    # Digest algorithm that produced `entry_hash` (e.g. "SHA-256"), so a future move to SHA-384
    # leaves historical entries verifiable under the algorithm they were actually written with.
    ("hash_algo", sa.Text()),
    # Signature algorithm (e.g. "ED25519"); null until Wave 1.2 signs entries.
    ("sig_alg", sa.Text()),
    # Version-pinned KMS key identity (ADR-0009), so rotation does not invalidate old entries.
    ("key_id", sa.Text()),
    # Which field set went into the preimage. Distinct from the encoding version: the encoding is
    # RFC 8785 JCS (platform.crypto.canonical), this names *what was fed to it*.
    ("preimage_version", sa.Integer()),
    # Raw signature bytes over (sequence || prev_entry_hash || entry_hash) — ADR-0003 §1.
    # BYTEA rather than text: no base64 layer to disagree about when verifying.
    ("signature", sa.LargeBinary()),
    # Reference to the external anchor (RFC-3161 token in WORM) covering this entry's Merkle
    # root; populated asynchronously by Wave 1.3.
    ("anchor_ref", sa.Text()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, type_, nullable=True), schema=_SCHEMA)


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column(_TABLE, name, schema=_SCHEMA)
