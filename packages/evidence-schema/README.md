# packages/evidence-schema

The canonical evidence and case data model.

## Purpose

Defines the shared shape that every evidence domain normalizes into, and that the `case-management` and `investigation` modules operate on. This is the contract that lets a genuinely cross-domain platform exist without every module needing to understand every other domain's native format — and what keeps that true if a module is later extracted into its own service.

The full design specification — core object, chain-of-custody model, evidence categories (including mobile forensics, blockchain intelligence, drone/IoT, and cloud evidence), entity/relationship types, validation rules, and example objects — lives in [docs/canonical-evidence-model.md](../../docs/canonical-evidence-model.md). This package is that design's implementation; keep them in sync.

## Status

Placeholder. No code yet — this package is the first thing to build in Phase 1 (see `docs/roadmap.md`), before any domain module depends on it.
