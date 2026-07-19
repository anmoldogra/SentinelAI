# modules/ingestion

Generic evidence intake and normalization.

## Purpose

The common front door for evidence entering the platform: validates incoming data, normalizes it into the canonical evidence model (`packages/evidence-schema`), and persists it with source, timestamp, and integrity metadata before handing off to `case-management` for linking. The domain-specific modules (`osint`, `social-media`, `forensics`, `threat-intel`) publish into this pipeline rather than each implementing their own normalization/persistence.

## Extraction path

Likely the first module worth extracting if ingestion volume ever becomes the platform's bottleneck (see `apps/server/README.md` for the general recipe) — it has the clearest single responsibility and the most naturally async workload.

## Status

Placeholder. No code yet — see `docs/roadmap.md` Phase 1.
