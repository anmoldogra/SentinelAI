# Security Policy

SentinelAI handles investigation evidence that may include sensitive and personally identifiable information (digital forensics artifacts, OSINT findings, social media data, threat intelligence). Security issues are treated as high priority regardless of what phase the codebase is in.

## Reporting a Vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

Preferred channel: use GitHub's private vulnerability reporting for this repository (repository **Security** tab → **Report a vulnerability**). This opens a private advisory visible only to maintainers until a fix is ready.

If private reporting is not available to you, contact the maintainers directly through a private channel rather than the public issue tracker.

Please include:
- A description of the issue and its potential impact
- Steps to reproduce (proof-of-concept if possible)
- Affected component (app/service/package path)

## Response

As this project is in early/pre-production stages, response times are best-effort. This section will be updated with formal SLAs once the platform has production deployments and an on-call rotation (see `docs/roadmap.md`).

## Scope

This policy currently covers this repository's code and configuration. Once cloud infrastructure exists (`infra/terraform`, `infra/kubernetes`), this document will be extended to cover deployed environments and disclosure scope explicitly.
