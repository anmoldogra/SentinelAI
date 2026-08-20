"""Unit tests for the migration-currency check (implementation-wave-1.md §9, W1-11).

These need no database: the *expected* side reads the real Alembic script directories on disk, and
the comparison semantics are pure. The applied-vs-expected behaviour against a live Postgres is
covered by ``tests/integration/test_migrations.py``.
"""

from __future__ import annotations

import ast

import pytest

from sentinelai.platform.migrations.currency import (
    MIGRATION_SCHEMAS,
    MigrationStatus,
    SchemaRevision,
    expected_heads,
    script_directory,
)

# --- script-directory discovery ---------------------------------------------


@pytest.mark.parametrize("schema", MIGRATION_SCHEMAS)
def test_every_schema_resolves_to_a_real_script_directory(schema: str) -> None:
    directory = script_directory(schema)
    assert directory.is_dir()
    assert (directory / "env.py").is_file()
    assert (directory / "versions").is_dir()


@pytest.mark.parametrize("schema", MIGRATION_SCHEMAS)
def test_every_schema_declares_exactly_one_head(schema: str) -> None:
    """A branched history would make "current" ambiguous and break the DAG-ordered runner."""
    heads = expected_heads(schema)
    assert len(heads) == 1, f"{schema} has {len(heads)} heads: {heads}"


def test_the_schema_list_matches_the_migration_runner_order() -> None:
    """currency.py and scripts/migrate.sh must not drift apart."""
    script = (script_directory("platform").parents[2] / ".." / "scripts" / "migrate.sh").resolve()
    text = script.read_text(encoding="utf-8")
    ordered = [s for s in text.split("SCHEMAS=(", 1)[1].split(")", 1)[0].split() if s]
    assert tuple(ordered) == MIGRATION_SCHEMAS


# --- comparison semantics ---------------------------------------------------


def test_a_schema_at_head_is_current() -> None:
    assert SchemaRevision("platform", ("abc",), ("abc",)).is_current is True


def test_a_schema_missing_a_revision_is_not_current() -> None:
    """The database sits on an older revision than the scripts declare."""
    assert SchemaRevision("ingestion", ("old",), ("new",)).is_current is False


def test_an_unmigrated_schema_is_not_current() -> None:
    """No alembic_version row at all — stale, not an error."""
    assert SchemaRevision("ingestion", (), ("new",)).is_current is False


def test_a_schema_ahead_of_the_scripts_is_not_current() -> None:
    """Code rolled back but the database was not — equally a mismatch."""
    assert SchemaRevision("osint", ("newer",), ()).is_current is False


def test_status_is_current_only_when_every_schema_is() -> None:
    good = SchemaRevision("platform", ("a",), ("a",))
    bad = SchemaRevision("ingestion", ("a",), ("b",))
    assert MigrationStatus((good, good)).is_current is True
    assert MigrationStatus((good, bad)).is_current is False


def test_status_names_every_stale_schema_not_just_the_first() -> None:
    status = MigrationStatus(
        (
            SchemaRevision("platform", ("a",), ("a",)),
            SchemaRevision("ingestion", ("a",), ("b",)),
            SchemaRevision("osint", (), ("c",)),
        )
    )
    assert status.stale == ("ingestion", "osint")


def test_summary_reports_only_stale_schemas_with_their_revisions() -> None:
    status = MigrationStatus(
        (
            SchemaRevision("platform", ("a",), ("a",)),
            SchemaRevision("ingestion", (), ("b",)),
        )
    )
    assert status.summary() == {"ingestion": "none -> b"}


def test_an_empty_status_is_trivially_current() -> None:
    assert MigrationStatus(()).is_current is True


# --- downgrade completeness (W1-16) -----------------------------------------


def _migration_files() -> list:
    return sorted(
        path
        for schema in MIGRATION_SCHEMAS
        for path in (script_directory(schema) / "versions").glob("*.py")
    )


def test_every_migration_defines_upgrade_and_downgrade() -> None:
    for path in _migration_files():
        names = {
            node.name
            for node in ast.parse(path.read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef)
        }
        assert {"upgrade", "downgrade"} <= names, f"{path.name} is missing a migration function"


def test_no_migration_has_a_pass_only_downgrade() -> None:
    """CLAUDE.md rule 9 / W1-16: every migration has a real, working downgrade — not `pass`."""
    offenders = []
    for path in _migration_files():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not (isinstance(node, ast.FunctionDef) and node.name == "downgrade"):
                continue
            body = [
                statement
                for statement in node.body
                if not (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)  # docstring
                )
            ]
            if not body or all(isinstance(statement, ast.Pass) for statement in body):
                offenders.append(path.name)
    assert offenders == []


def test_migration_files_are_discovered_at_all() -> None:
    """Guards the two checks above from silently passing on an empty file list."""
    assert len(_migration_files()) >= len(MIGRATION_SCHEMAS)
