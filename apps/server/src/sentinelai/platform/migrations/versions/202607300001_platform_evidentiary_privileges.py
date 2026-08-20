"""platform schema — evidentiary-table privileges on audit_log (ADR-0004, part 2).

Grants ``INSERT, SELECT`` to ``sentinel_append`` and revokes ``UPDATE, DELETE`` from the
application roles on ``platform.audit_log`` (role-existence-guarded — see
``platform.db.privileges``). The append-only trigger from ``202607280001_platform_append``
is the unconditional backstop; this migration is the privilege layer above it.

Revision ID: 202607300001_platform_privs
Revises: 202607280001_platform_append
"""

from __future__ import annotations

from alembic import op

from sentinelai.platform.db.privileges import (
    grant_evidentiary_privileges_sql,
    revoke_evidentiary_privileges_sql,
)

revision = "202607300001_platform_privs"
down_revision = "202607280001_platform_append"
branch_labels = None
depends_on = None

_SCHEMA = "platform"
_TABLES = ("audit_log",)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(grant_evidentiary_privileges_sql(_SCHEMA, table))


def downgrade() -> None:
    for table in _TABLES:
        op.execute(revoke_evidentiary_privileges_sql(_SCHEMA, table))
