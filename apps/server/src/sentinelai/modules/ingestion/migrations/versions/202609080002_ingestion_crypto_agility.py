"""ingestion schema — cryptographic agility columns on evidence_custody_events (ADR-0003 §5).

The custody half of modernization Wave 1.1; ``202609080001_platform_agility`` is the audit half.
The two are deliberately identical in column set, type, and nullability: ADR-0003 treats the
custody and audit ledgers as one integrity subsystem, so the Verification Engine (Wave 1.4) reads
both through one schema rather than special-casing each. They are separate migrations only
because each module owns its own schema and history (database-design.md §5, §11) — a single
migration touching both would violate that boundary even though the change is one logical unit.

Nullability, staging, backfill, and privileges: see ``202609080001_platform_agility``'s docstring;
the reasoning is identical and is not repeated here.

Revision ID: 202609080002_ingestion_agility
Revises: 202608300002_ingestion_seed
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "202609080002_ingestion_agility"
down_revision = "202608300002_ingestion_seed"
branch_labels = None
depends_on = None

_SCHEMA = "ingestion"
_TABLE = "evidence_custody_events"

_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[Any]], ...] = (
    ("hash_algo", sa.Text()),
    ("sig_alg", sa.Text()),
    ("key_id", sa.Text()),
    ("preimage_version", sa.Integer()),
    ("signature", sa.LargeBinary()),
    ("anchor_ref", sa.Text()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column(_TABLE, sa.Column(name, type_, nullable=True), schema=_SCHEMA)


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column(_TABLE, name, schema=_SCHEMA)
