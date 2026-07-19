# apps/server/entrypoints/worker

The background/asynchronous job entrypoint — same `modules/` and `platform/` as `entrypoints/http`, run as a separate process so long-running or scheduled work (ingestion pipeline processing, OSINT/social media polling, threat feed sync, AI correlation runs, notification delivery) doesn't block request/response handling.

Because it shares a codebase with `entrypoints/http` rather than being a separate app, adding a background job never requires touching a second repository or dependency set — you write the handler in the relevant module and register it here.

## Status

Placeholder. No code yet.
