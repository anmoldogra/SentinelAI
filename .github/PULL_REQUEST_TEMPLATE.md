## What & why

<!-- What does this PR change, and why? Link an issue/ADR/task if relevant. -->

## Scope

- [ ] This PR touches only the app/service/package(s) listed in the title/commits
- [ ] If it adds a new service, package, datastore, or external dependency, an ADR is included/linked (`docs/adr/`)
- [ ] Small and focused — one reviewable concern, not several bundled together

## How was this tested?

<!-- Unit/integration tests added, manual verification steps, or "N/A — docs only" -->

## Engineering DoD

<!-- engineering-standards.md §17. Every box must be true, not aspirational. -->

- [ ] Implements the frozen spec — no redesign, no new tech, no undocumented ADR (or the blocker was raised as one)
- [ ] `ruff check`, `ruff format --check`, `mypy src`, and `lint-imports` are green locally
- [ ] Tests written and green; coverage floor met; invariants tested
- [ ] **No `TODO`, `FIXME`, stub, or `pass`-only function presented as complete** — incomplete work is stated as incomplete in the PR body
- [ ] Docs / ADR / event catalog updated **in this same change**, not deferred
- [ ] Structured, PII-safe logging — no secrets, tokens, presigned URLs, or evidence content in logs

## PR DoD

- [ ] Conventional Commit messages (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`)
- [ ] Links the ADR / roadmap task this implements
- [ ] Correct reviewer count (**2 for Tier-0** surfaces: evidence, custody, audit, auth, crypto)
- [ ] Full CI green — every gate, no bypass (a bypass needs a labeled, time-boxed, board-approved exception)

## Domain guardrails

<!-- Delete any line that this PR genuinely cannot affect. -->

- [ ] **Evidence & custody:** no in-place mutation of `ingestion.evidence` or the custody ledger — state transitions are append-only records (ADR-0004, ADR-0015)
- [ ] **Migrations:** every new migration has a real, working `downgrade()`; stays inside its own module schema; no cross-schema FK
- [ ] **Events:** every business write that announces a fact does so via an outbox insert **in the same transaction**; every consumer performs the Inbox claim first; new event types are added to `event-driven-architecture.md` §25 in this PR
- [ ] **Boundaries:** no deep import of another module's internals — cross-module access goes through `public.py`
- [ ] **Security:** no weakened authorization; parameterized queries only; any new deletion/purge path checks `legal_hold` first
- [ ] **Secrets:** no `.env`, key, credential, or evidence payload committed
