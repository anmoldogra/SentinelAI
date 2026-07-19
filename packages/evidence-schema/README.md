# packages/evidence-schema

The canonical evidence and case data model.

## Purpose

Defines the shared shape that all domains (forensics, OSINT, threat intel, social media) normalize into, and that the `case-management` and `investigation` modules operate on. This is the contract that lets a genuinely cross-domain platform exist without every module needing to understand every other domain's native format — and what keeps that true if a module is later extracted into its own service.

## Status

Placeholder. No code yet — this package is the first thing to build in Phase 1 (see `docs/roadmap.md`), before any domain module depends on it.
