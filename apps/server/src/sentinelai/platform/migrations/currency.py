"""Migration-currency check — implementation-wave-1.md §9's "migrations-current" startup input.

Answers one question without running anything: **is the database's schema at the head revision
each module's migration history declares?** A pod that boots against a database missing a
revision would serve requests against a schema its code does not match, so `/startupz` gates on
this (W1-11).

Read-only and side-effect free: it compares the ``alembic_version`` row inside each module's own
schema (database-design.md §11's per-module history) against the head of that module's script
directory. It never creates a schema, never stamps, and never applies a revision — applying is
``scripts/migrate.sh`` / the ArgoCD PreSync job's job (deployment-architecture Part 5), never the
application's.

Script directories are resolved from this package's location rather than ``alembic.ini``, so the
check is independent of the process's working directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

# database-design.md §5 / deployment-architecture Part 5 DAG order — the same order
# scripts/migrate.sh applies. Each name is both the schema and its module directory.
MIGRATION_SCHEMAS: tuple[str, ...] = (
    "platform",
    "ingestion",
    "osint",
    "threat_intel",
    "forensics",
    "social_media",
    "case_management",
    "investigation",
    "notification",
)

_SENTINELAI = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class SchemaRevision:
    """One module's applied vs expected head revisions (both sorted for comparison)."""

    schema: str
    applied: tuple[str, ...]
    expected: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return self.applied == self.expected


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    """The per-schema result of a currency check."""

    schemas: tuple[SchemaRevision, ...]

    @property
    def is_current(self) -> bool:
        return all(revision.is_current for revision in self.schemas)

    @property
    def stale(self) -> tuple[str, ...]:
        """Names of the schemas that are not at their head revision."""
        return tuple(r.schema for r in self.schemas if not r.is_current)

    def summary(self) -> dict[str, str]:
        """Compact, log-safe rendering: schema → ``applied -> expected`` (revision ids only)."""
        return {
            r.schema: f"{','.join(r.applied) or 'none'} -> {','.join(r.expected) or 'none'}"
            for r in self.schemas
            if not r.is_current
        }


def script_directory(schema: str) -> Path:
    """Filesystem location of ``schema``'s Alembic script directory."""
    if schema == "platform":
        return _SENTINELAI / "platform" / "migrations"
    return _SENTINELAI / "modules" / schema / "migrations"


def expected_heads(schema: str) -> tuple[str, ...]:
    """Head revision id(s) declared by ``schema``'s migration history (sorted)."""
    config = Config()
    config.set_main_option("script_location", str(script_directory(schema)))
    return tuple(sorted(ScriptDirectory.from_config(config).get_heads()))


def applied_heads(connection: Connection, schema: str) -> tuple[str, ...]:
    """Revision id(s) recorded in ``schema``'s ``alembic_version`` table (sorted).

    Returns an empty tuple when the schema or its version table does not exist — an
    un-migrated database, which is stale rather than an error.
    """
    context = MigrationContext.configure(connection, opts={"version_table_schema": schema})
    return tuple(sorted(context.get_current_heads()))


async def check_migrations_current(
    engine: AsyncEngine, schemas: tuple[str, ...] = MIGRATION_SCHEMAS
) -> MigrationStatus:
    """Compare every module schema's applied revision against its script head.

    Raises whatever the driver raises if the database is unreachable — the caller decides
    whether that blocks startup (it is reported, not fatal, per the startup gate in
    ``entrypoints/http/main.py``).
    """
    revisions: list[SchemaRevision] = []
    async with engine.connect() as connection:
        for schema in schemas:
            applied = await connection.run_sync(applied_heads, schema)
            revisions.append(
                SchemaRevision(schema=schema, applied=applied, expected=expected_heads(schema))
            )
    return MigrationStatus(schemas=tuple(revisions))


__all__ = [
    "MIGRATION_SCHEMAS",
    "MigrationStatus",
    "SchemaRevision",
    "applied_heads",
    "check_migrations_current",
    "expected_heads",
    "script_directory",
]
