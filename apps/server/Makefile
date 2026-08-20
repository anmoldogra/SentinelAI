# SentinelAI backend (apps/server) — developer entrypoints.
# See docs/backend-implementation-guide.md for the rules these gates enforce.

.DEFAULT_GOAL := help
PY := python

# Force UTF-8 I/O for all tooling. Without this, import-linter's rich spinner (a non-ASCII
# emoji) crashes under a legacy Windows codepage (cp1252) whenever output is piped or redirected
# — e.g. CI log capture or pre-commit. No-op on already-UTF-8 platforms (Linux CI).
export PYTHONUTF8 := 1
export PYTHONIOENCODING := utf-8

# Platform coverage floor — wave-1 §17's Tier-0 mandate, now met and enforced. Kept in one
# place so the Makefile and .github/workflows/ci.yml agree.
COVERAGE_FLOOR := 90

.PHONY: help install lint format typecheck lint-imports test test-unit test-integration \
        test-migrations hooks check run-api run-worker migrate compose-up compose-down \
        compose-logs

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	 awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install the package with dev extras (editable)
	$(PY) -m pip install -e ".[dev]"

lint: ## Ruff lint
	ruff check src tests

format: ## Ruff format (write)
	ruff format src tests
	ruff check --fix src tests

typecheck: ## mypy strict type check
	mypy src

lint-imports: ## Enforce the module dependency DAG
	lint-imports

test: ## Run the test suite
	pytest

test-migrations: ## Prove every migration is reversible (upgrade head -> downgrade base)
	pytest tests/integration/test_migrations.py -v

test-unit: ## Unit suite + the platform coverage floor CI enforces
	pytest tests/unit --cov=sentinelai.platform --cov-report=term-missing --cov-fail-under=$(COVERAGE_FLOOR)

test-integration: ## Integration suite (needs Postgres/Redis/MinIO/Vault reachable)
	pytest tests/integration -v

hooks: ## Install the pre-commit hooks that mirror the CI gates
	cd ../.. && pre-commit install

check: lint typecheck lint-imports test ## All CI gates — a change is not done until this passes

run-api: ## Run the HTTP entrypoint (dev, reload)
	uvicorn sentinelai.entrypoints.http.main:app --reload --host 0.0.0.0 --port 8000

run-worker: ## Run the arq worker entrypoint (dev)
	arq sentinelai.entrypoints.worker.main.WorkerSettings

migrate: ## Apply all per-module Alembic migrations in DAG order
	./scripts/migrate.sh

compose-up: ## Start the full dev stack (postgres, redis, api, worker, web on :5173)
	docker compose -f docker-compose.dev.yml up -d --build

compose-down: ## Stop the dev stack
	docker compose -f docker-compose.dev.yml down

compose-logs: ## Tail logs from the dev stack (e.g. make compose-logs SERVICE=web)
	docker compose -f docker-compose.dev.yml logs -f $(SERVICE)
