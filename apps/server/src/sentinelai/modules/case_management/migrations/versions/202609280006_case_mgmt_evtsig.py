"""case_management schema — ADR-0007 event signature columns (Wave 2.3).

Adds ``signature``, ``key_id`` and ``sig_alg`` to ``case_management.outbox_events``.

Additive and nullable, so this is safe against a table with rows in it and needs no backfill. Rows
written before this migration keep ``NULL``, which is the truthful value: they were never signed,
and signing them now would attest to bytes nobody witnessed at publication. The dispatcher's
permissive verification mode carries such rows through the migration; strict mode refuses them.

One migration per module rather than one reaching across every schema: each module owns its schema
and its own Alembic chain (database-design.md §5), and the ArgoCD PreSync job applies those chains
separately (deployment-architecture.md Part 5).

Alembic stores ``version_num`` in a ``varchar(32)``, so a longer revision id fails the UPDATE at
the end of the migration rather than anything in its body. Hence the terse ``_evtsig`` suffix.

Revision ID: 202609280006_case_mgmt_evtsig
Revises: 202609250006_case_mgmt_idx
"""

from __future__ import annotations

from sentinelai.platform.migrations._event_tables import (
    add_outbox_signature_columns,
    drop_outbox_signature_columns,
)

revision = "202609280006_case_mgmt_evtsig"
down_revision = "202609250006_case_mgmt_idx"
branch_labels = None
depends_on = None

_SCHEMA = "case_management"


def upgrade() -> None:
    add_outbox_signature_columns(_SCHEMA)


def downgrade() -> None:
    drop_outbox_signature_columns(_SCHEMA)
