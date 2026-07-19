# modules/notification

Alerting and notification delivery.

## Purpose

Notifies analysts of significant events — new AI-surfaced correlations, case status changes, SLA breaches — across delivery channels (email, chat, webhook).

## Extraction path

Follows the general recipe in `apps/server/README.md`. Naturally event-driven already (reacts to events from other modules via `platform`'s event bus), so it's one of the more mechanical extractions when the time comes.

## Status

Placeholder. No code yet — see `docs/roadmap.md` Phase 3.
