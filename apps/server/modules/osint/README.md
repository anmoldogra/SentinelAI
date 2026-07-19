# modules/osint

Open-source intelligence collection.

## Purpose

Source-specific connectors for gathering open-source intelligence (public records, domain/IP intelligence, breach data, etc.), plus source reliability scoring. Normalized findings are handed to the `ingestion` module for entry into the canonical evidence model.

## Extraction path

A natural extraction candidate once external-source polling needs its own scaling/rate-limit isolation from the rest of the platform — see `apps/server/README.md`. Each connector already being a separable unit inside this module means the module-level extraction (not per-connector) is the practical granularity.

## Status

Placeholder. No code yet — see `docs/roadmap.md` Phase 2.
