# modules/case-management

Case lifecycle and chain of custody.

## Purpose

Owns the case record: creation, status/workflow, linking evidence from any domain to a case, chain-of-custody logging, and report generation/export. This is the system of record an investigation is ultimately judged against.

## Extraction path

Follows the general recipe in `apps/server/README.md`. Likely one of the *later* modules to extract in practice — nearly everything else links to a case, so extracting it early would mean every other module immediately needs a network call instead of a function call, before extraction is actually paying for itself.

## Status

Placeholder. No code yet — see `docs/roadmap.md` Phase 1.
