# modules/forensics

Digital forensic artifact processing.

## Purpose

Parses digital forensic artifacts (disk/memory images, file system metadata, logs, extracted documents) and maintains chain-of-custody records for them. This module's audit/integrity guarantees are a legal requirement, not just a technical one — see `docs/architecture.md` non-functional requirements. That requirement holds regardless of monolith vs. microservice; it's enforced by the module's own schema/audit log, not by deployment topology.

## Extraction path

Because chain-of-custody already forces this module to own an isolated, append-only audit trail (module boundary rule 2 in `apps/server/README.md`), extraction is unusually clean — the hardest part of separating this module (data isolation) is already done for compliance reasons, not as extraction prep.

## Status

Placeholder. No code yet — see `docs/roadmap.md` Phase 2.
