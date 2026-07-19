# modules/threat-intel

Threat intelligence feed consumption and correlation.

## Purpose

Ingests external threat intelligence feeds, manages indicators of compromise (IOCs), and provides threat actor/campaign context that the `investigation` module can correlate against case evidence.

## Extraction path

Follows the general recipe in `apps/server/README.md`. Feed ingestion volume/licensing constraints (some threat feeds require dedicated network egress or isolated credentials) are the most likely trigger for extracting this one early.

## Status

Placeholder. No code yet — see `docs/roadmap.md` Phase 2.
