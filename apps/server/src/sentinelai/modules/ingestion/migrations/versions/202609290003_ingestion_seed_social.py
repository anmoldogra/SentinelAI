"""ingestion schema — register the `social_media_intelligence` attribute schemas (CEM §6).

**Why an `ingestion` migration ships with the `social_media` connector.** `ingest_evidence`
refuses a `(schema_version, category, artifact_type)` triple absent from
`attribute_schema_registry` (CEM §13), and `202608300002_ingestion_seed` registered none for
`social_media_intelligence` — its baseline covered the formats the *first* connectors emitted. So
before this migration the `social_media` publish path could not succeed for any content kind: not a
gap to carry forward, a feature that could not work.

The registry belongs to `ingestion`, so the rows belong in `ingestion`'s Alembic chain — a module
may not migrate another module's schema (`database-design.md` §5), and the ArgoCD PreSync job
applies `ingestion` before the domain modules, so the entries exist by the time a connector calls.

The six types are CEM §6's `social_media_intelligence` row, verbatim, and nothing else. §12 makes
the registry additive, so a new type later is another migration rather than an edit to this one.

Revision ID: 202609290003_ingestion_seed_social
Revises: 202609280001_ingestion_evtsig
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202609290003_ingest_seed_social"
down_revision = "202609280001_ingestion_evtsig"
branch_labels = None
depends_on = None

_SCHEMA = "ingestion"

# The same namespace `202608300002_ingestion_seed` uses, so a triple's id is reproducible from
# its name in either migration — one derivation, so a re-seed cannot add a second row for a triple.
_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:sentinelai:ingestion:attribute-schema")
_SCHEMA_VERSION = "1.0.0"
_CATEGORY = "social_media_intelligence"

# CEM §6's artifact types for this category.
_ARTIFACT_TYPES: tuple[str, ...] = (
    "post",
    "profile_snapshot",
    "comment",
    "direct_message",
    "network_connection_snapshot",
    "media_upload",
)

_registry = sa.table(
    "attribute_schema_registry",
    sa.column("registry_id", postgresql.UUID(as_uuid=True)),
    sa.column("schema_version", sa.Text()),
    sa.column("category", sa.Text()),
    sa.column("artifact_type", sa.Text()),
    schema=_SCHEMA,
)


def _registry_id(artifact_type: str) -> uuid.UUID:
    return uuid.uuid5(_ID_NAMESPACE, f"{_SCHEMA_VERSION}:{_CATEGORY}:{artifact_type}")


def upgrade() -> None:
    op.bulk_insert(
        _registry,
        [
            {
                "registry_id": _registry_id(artifact_type),
                "schema_version": _SCHEMA_VERSION,
                "category": _CATEGORY,
                "artifact_type": artifact_type,
            }
            for artifact_type in _ARTIFACT_TYPES
        ],
    )


def downgrade() -> None:
    op.execute(
        _registry.delete().where(
            sa.and_(
                _registry.c.schema_version == _SCHEMA_VERSION,
                _registry.c.category == _CATEGORY,
                _registry.c.artifact_type.in_(_ARTIFACT_TYPES),
            )
        )
    )
