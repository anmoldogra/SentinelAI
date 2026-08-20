"""ingestion schema — evidentiary-table privileges on evidence + custody (ADR-0004, part 2).

Grants ``INSERT, SELECT`` to ``sentinel_append`` and revokes ``UPDATE, DELETE`` from the
application roles on ``ingestion.evidence`` and ``ingestion.evidence_custody_events``
(role-existence-guarded — see ``platform.db.privileges``). The append-only triggers from
``202607280002_ingestion_append`` are the unconditional backstop; this is the privilege layer.

Revision ID: 202607300002_ingestion_privs
Revises: 202607280002_ingestion_append
"""

from __future__ import annotations

from alembic import op

from sentinelai.platform.db.privileges import (
    grant_evidentiary_privileges_sql,
    revoke_evidentiary_privileges_sql,
)

revision = "202607300002_ingestion_privs"
down_revision = "202607280002_ingestion_append"
branch_labels = None
depends_on = None

_SCHEMA = "ingestion"
_TABLES = ("evidence", "evidence_custody_events")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(grant_evidentiary_privileges_sql(_SCHEMA, table))


def downgrade() -> None:
    for table in _TABLES:
        op.execute(revoke_evidentiary_privileges_sql(_SCHEMA, table))
