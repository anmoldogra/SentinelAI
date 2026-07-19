# .github/workflows

CI/CD pipeline definitions (GitHub Actions): lint, test, build, and deploy workflows.

## Status

`pr-validation.yml` is the current baseline — validates `docker-compose.yml` and scans PR diffs for committed secrets. It has no dependency on application code, so it runs from day one.

Because the backend is one deployable (`apps/server`, a modular monolith — see `docs/architecture.md`), Phase 1 needs one lint/test/build workflow for it plus one for `apps/web`, not one per domain module. A per-module workflow only becomes relevant if/when that module is extracted into its own service (see `apps/server/README.md`'s extraction path).
