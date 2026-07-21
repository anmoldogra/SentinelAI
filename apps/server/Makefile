# SentinelAI backend (apps/server) — developer entrypoints.
# See docs/backend-implementation-guide.md for the rules these gates enforce.

.DEFAULT_GOAL := help
PY := python

.PHONY: help install lint format typecheck lint-imports test check \
        run-api run-worker migrate compose-up compose-down

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

check: lint typecheck lint-imports test ## All CI gates — a change is not done until this passes

run-api: ## Run the HTTP entrypoint (dev, reload)
	uvicorn sentinelai.entrypoints.http.main:app --reload --host 0.0.0.0 --port 8000

run-worker: ## Run the arq worker entrypoint (dev)
	arq sentinelai.entrypoints.worker.main.WorkerSettings

migrate: ## Apply all per-module Alembic migrations in DAG order
	./scripts/migrate.sh

compose-up: ## Start the local dev stack (postgres, redis, api, worker)
	docker compose -f docker-compose.dev.yml up -d --build

compose-down: ## Stop the local dev stack
	docker compose -f docker-compose.dev.yml down
