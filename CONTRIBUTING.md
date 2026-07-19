# Contributing to SentinelAI

## Current phase

The repository is in its scaffold phase (see `docs/roadmap.md`) — there is no application code to contribute to yet. Contributions right now are documentation, architecture (ADRs), and infrastructure scaffolding. This document sets the process now so it's already in place once Phase 1 implementation starts.

## Branching & merging

- `main` is always deployable/reviewable — no direct commits.
- Work happens on short-lived branches off `main`: `<type>/<short-description>`, e.g. `feat/case-evidence-linking`, `fix/ingestion-timestamp-bug`, `docs/update-roadmap`.
- Open a PR early (draft is fine). Rebase on `main` rather than merging `main` into your branch.
- Squash-merge to keep `main` history one commit per logical change.

## Commit messages & PR titles

Follow [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, e.g. `feat(case-management-service): add evidence linking endpoint`. Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`. The `scope` should usually be the app/service/package folder name.

## Code review

- Every PR needs at least one approval from the relevant CODEOWNERS group (see `.github/CODEOWNERS`) before merge.
- PRs that touch `docs/architecture.md`, add a new service/package, or introduce a new external dependency should include or reference an ADR (`docs/adr/`, see `0001-record-architecture-decisions.md`).
- Reviewers own quality, not just approval — if something needs simplification, say so before approving.

## Local setup

```bash
git clone <repo>
cd SentinelAI
docker compose up -d   # provisions Postgres, Redis, Redpanda, MinIO, Qdrant
```

Per-service setup instructions will be added to each service's `README.md` as it moves from placeholder to real code.

## Reporting bugs / requesting features

Use the issue templates under `.github/ISSUE_TEMPLATE/`. For security vulnerabilities, do **not** open a public issue — see `SECURITY.md`.

## Questions

If a decision isn't covered here or in `docs/`, raise it — don't guess and diverge. Architecturally significant answers should end up as an ADR so the next person doesn't have to ask again.
