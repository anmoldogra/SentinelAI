"""ingestion schema — seed the baseline attribute-schema registry (CEM §6, §12).

``ingestion.attribute_schema_registry`` is the runtime gate on evidence creation:
``IngestionService`` rejects any submission whose ``(schema_version, category,
artifact_type)`` triple is not registered (``AttributeSchemaRepository.is_registered``).
The table shipped empty from ``202607210002_ingestion``, so *no* evidence could be
ingested. This seeds the baseline triples so a fresh ``scripts/migrate.sh`` yields a
usable ingestion path.

Registry ids are derived deterministically (UUIDv5 over a stable URN per triple) rather
than generated per-run, so the same triple carries the same ``registry_id`` in every
environment and ``downgrade()`` can remove exactly the rows this migration created —
leaving any triple registered later untouched.

All five triples register under ``schema_version`` 1.0.0. Per CEM §12 that is not a
version bump: the registry was previously empty, so this is its initial population.

Revision ID: 202608300002_ingestion_seed
Revises: 202607300002_ingestion_privs
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202608300002_ingestion_seed"
down_revision = "202607300002_ingestion_privs"
branch_labels = None
depends_on = None

_SCHEMA = "ingestion"

# Namespace for the deterministic registry ids. Any stable, project-owned URN works; this one
# is never resolved over the network — it exists only to make UUIDv5 derivation reproducible.
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:sentinelai:ingestion:attribute-schema")

_SCHEMA_VERSION = "1.0.0"

# (category, artifact_type) — CEM §6, extended in this change with the tool-specific types the
# first ingestion connectors emit (DJI CFID/DATCON flight logs, Oxygen Forensic extractions).
_BASELINE: tuple[tuple[str, str], ...] = (
    ("drone_iot", "cfid_log"),
    ("drone_iot", "datcon_log"),
    ("mobile_forensics", "oxygen_extraction"),
    ("cloud_evidence", "oxygen_cloud_extraction"),
    ("digital_forensics", "forensic_image"),
)

_registry = sa.table(
    "attribute_schema_registry",
    sa.column("registry_id", postgresql.UUID(as_uuid=True)),
    sa.column("schema_version", sa.Text()),
    sa.column("category", sa.Text()),
    sa.column("artifact_type", sa.Text()),
    schema=_SCHEMA,
)


def _registry_id(category: str, artifact_type: str) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, f"{_SCHEMA_VERSION}:{category}:{artifact_type}")


def upgrade() -> None:
    op.bulk_insert(
        _registry,
        [
            {
                "registry_id": _registry_id(category, artifact_type),
                "schema_version": _SCHEMA_VERSION,
                "category": category,
                "artifact_type": artifact_type,
            }
            for category, artifact_type in _BASELINE
        ],
    )


def downgrade() -> None:
    op.execute(
        _registry.delete().where(
            _registry.c.registry_id.in_(
                [_registry_id(category, artifact_type) for category, artifact_type in _BASELINE]
            )
        )
    )
