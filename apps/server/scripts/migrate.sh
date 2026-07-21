#!/usr/bin/env bash
# Apply every module's per-schema Alembic migrations in database-design.md §5 /
# deployment-architecture.md Part-5 DAG order:
#   platform -> ingestion -> {domain modules} -> investigation -> notification
#
# Each schema has its own Alembic history + alembic_version table (inside its own
# schema). env.py creates the schema before its version table, and reads the DB URL
# from platform/config.py settings. Run from anywhere: `apps/server/scripts/migrate.sh`.
set -euo pipefail

cd "$(dirname "$0")/.."  # -> apps/server

SCHEMAS=(
  platform
  ingestion
  osint
  threat_intel
  forensics
  social_media
  case_management
  investigation
  notification
)

CMD="${1:-upgrade}"          # upgrade (default) | downgrade
TARGET="${2:-head}"          # head (default) | base | <revision>

for schema in "${SCHEMAS[@]}"; do
  echo ">> alembic -n ${schema} ${CMD} ${TARGET}"
  alembic -c alembic.ini -n "${schema}" "${CMD}" "${TARGET}"
done

echo "Done: ${CMD} ${TARGET} across ${#SCHEMAS[@]} schemas."
