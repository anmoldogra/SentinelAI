# 1. Record Architecture Decisions

## Status

Accepted

## Context

SentinelAI spans multiple domains (forensics, OSINT, threat intel, social media, case management) and multiple services, each of which will accumulate architecturally significant decisions over time — datastore choices, service boundaries, auth strategy, AI model strategy, and more. Without a record, the reasoning behind these decisions gets lost, and future contributors (human or AI) re-litigate settled questions or unknowingly violate constraints that existed for a reason.

## Decision

We will use Architecture Decision Records (ADRs), as described by Michael Nygard, to record any architecturally significant decision made in this project.

Each ADR is a short markdown file in `docs/adr/`, numbered sequentially, following this structure:

```
# <number>. <title>

## Status
Proposed | Accepted | Superseded by ADR-000X

## Context
What is the issue that we're seeing that motivates this decision?

## Decision
What is the change we're actually proposing/doing?

## Consequences
What becomes easier or harder as a result of this change?
```

## Consequences

- Every non-trivial architectural choice (new service boundary, datastore, external dependency, auth model, AI provider) should be captured as an ADR before or shortly after implementation.
- ADRs are immutable once accepted — a changed decision gets a new ADR that supersedes the old one, not an edit to history.
- This adds a small amount of process overhead but keeps `docs/architecture.md` honest and gives future contributors the "why," not just the "what."
