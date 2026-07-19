# apps/server

The SentinelAI backend — a **modular monolith**. One codebase, one deployable unit, one database — internally organized into the same domain-bounded modules the platform was originally designed around, so it can be split into independent services later without redesigning anything.

## Why a monolith for Phase 1

With a single developer, network hops between domains, per-service deploys, and distributed transactions are pure overhead with no offsetting benefit (no second team to isolate, no divergent scaling need yet). See `docs/architecture.md` for the full reasoning and the point at which extraction starts to pay off.

## Structure

```
apps/server/
  entrypoints/
    http/        # API entrypoint (owns routing, auth enforcement — formerly apps/api-gateway)
    worker/       # background/async job entrypoint (formerly apps/worker)
  modules/
    ingestion/
    osint/
    threat-intel/
    forensics/
    social-media/
    case-management/
    investigation/
    notification/
  platform/       # cross-cutting plumbing shared by both entrypoints (see platform/README.md)
```

Both `entrypoints/http` and `entrypoints/worker` import from the same `modules/` and `platform/` — they are two processes of **one** deployable unit, not two apps. They can still be deployed and scaled as separate containers/replicas (see `docker-compose.yml`); they just share one codebase, one dependency set, and one release.

## Module boundary rules (non-negotiable)

These rules are what make extraction later a mechanical move instead of a rewrite. They're conventions to enforce (via code review now; via a dependency-boundary lint rule once a language is chosen — see the open ADR in `docs/architecture.md`), not runtime constraints the monolith enforces for you:

1. **A module may only be imported through its own public interface** (e.g. a top-level `index`/`public` entry per module) — never by reaching into another module's internal files, repository layer, or types.
2. **A module owns its own database schema/table namespace.** No cross-module SQL joins, no reading another module's tables directly. Cross-module reads go through that module's public interface.
3. **Cross-module side effects go through `platform`'s event bus, not direct calls where an event fits better** (e.g. "evidence ingested" → investigation module reacts). In Phase 1 this is an in-process pub/sub; the transport is swappable later without changing module code (see extraction path below).
4. **Modules depend on the shared `packages/evidence-schema` contract, not on each other's internal types**, for anything crossing a module boundary.

## Extraction path (general recipe)

Every module in `modules/` can become an independent service this way:

1. Copy `apps/server/modules/<name>/` into a new deployable (`services/<name>-service/`), give it its own entrypoint and dependency manifest.
2. Move that module's schema/tables to their own database instance (they were already isolated within the shared Postgres — see rule 2 above — so this is a data migration, not a redesign).
3. Replace the in-process adapter other modules used to call this module's public interface with a network client (HTTP/gRPC). Because callers only ever depended on the public interface (rule 1), their code doesn't change — only the adapter implementation backing that interface does.
4. Point the module's event-bus subscriptions at the real broker (Redpanda is already provisioned in `docker-compose.yml` for exactly this) instead of the in-process bus.
5. Remove the module from `apps/server/modules/` and its wiring in `apps/server/platform`'s composition root; add the new service to `docker-compose.yml` / `infra/kubernetes`.

Module-specific extraction notes are in each module's own `README.md`.

## Status

Placeholder. No code yet — see `docs/roadmap.md`.
