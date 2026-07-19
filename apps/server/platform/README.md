# apps/server/platform

Cross-cutting plumbing shared by both entrypoints (`entrypoints/http`, `entrypoints/worker`): database connection/migration management, the in-process event bus, auth/session handling, HTTP routing/middleware, and the composition root that wires modules together.

This is monolith-internal infrastructure — not reusable outside `apps/server`, which is why it lives here rather than in `packages/` (compare `packages/shared-utils`, which *is* meant to be imported by other apps).

## Event bus (extraction-relevant)

Phase 1 uses an in-process publish/subscribe implementation so modules can react to each other's events without a message broker running. Its interface is intentionally broker-shaped (publish a named event with a payload; subscribe by event name) so that swapping the in-process implementation for a Redpanda-backed one — when a module is extracted per `apps/server/README.md`'s extraction path — is an adapter swap, not a rewrite of module code.

## Status

Placeholder. No code yet.
