# apps/server/entrypoints/http

The API entrypoint — routing, auth enforcement, and request/response contracts for `apps/web` and any external/programmatic clients (`packages/sdk`). Mounts every module in `apps/server/modules/` behind one HTTP surface.

This folder absorbs what would have been a separate `api-gateway` service — with only one backend in Phase 1, a network hop between "gateway" and "the services it routes to" has no purpose. If the platform later has multiple independently-deployed services again (post-extraction), a dedicated gateway/BFF can be reintroduced in front of them; this entrypoint's routing logic is exactly what would move there.

## Status

Placeholder. No code yet — see `docs/roadmap.md` Phase 1.
