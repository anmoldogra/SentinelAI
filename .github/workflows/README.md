# .github/workflows

CI/CD pipeline definitions (GitHub Actions).

## Workflows

| File | Scope | Runs on |
|---|---|---|
| `ci.yml` | The `apps/server` governance pipeline (implementation-wave-1.md §17): format, lint, types, architecture, unit + coverage, integration against real services, security/dependency scanning, SBOM, container build, migration round-trip. | PRs to `main`, pushes to `main` |
| `pr-validation.yml` | Repository-level checks with no dependency on application code: `docker compose config` validation and TruffleHog secret scanning. | PRs to `main` |

The two are deliberately separate: `pr-validation.yml` runs from day one against any change
(including docs-only), while `ci.yml` exercises the backend toolchain.

## Branch protection

`ci-passed` is the single required status check. It aggregates every hard gate, so branch
protection needs one rule rather than one per job — and adding a gate to `ci.yml` does not
require touching the protection settings. It fails if any dependency was skipped or cancelled,
not only when one failed outright.

## Service containers

Postgres and Redis run as GitHub Actions `services:`. MinIO and Vault are started with
`docker run` in a step instead, because both need command arguments (`server /data`,
dev-mode listener) and a service container cannot be given a command.

The integration job asserts that **no integration test was skipped**. These tests skip
themselves when their dependency is unreachable — which is right locally, but in CI a skip
would be indistinguishable from a pass, so a service that failed to start must fail the build.

## Not yet implemented

Image signing. `deployment-architecture.md` Part 5 requires every deployable image to be
cosign-signed from an approved base before it may be deployed. Signing-key infrastructure is not
bootstrapped, so `ci.yml` builds and CVE-scans the image but does **not** push or sign it —
nothing it produces is deployable. A `release.yml` with signing, pushing, and the GitOps deploy
gates is a separate increment.

Because the backend is one deployable (`apps/server`, a modular monolith — see
`docs/architecture.md`), Phase 1 needs one lint/test/build workflow for it plus one for
`apps/web`, not one per domain module. A per-module workflow only becomes relevant if/when that
module is extracted into its own service (see `apps/server/README.md`'s extraction path).
