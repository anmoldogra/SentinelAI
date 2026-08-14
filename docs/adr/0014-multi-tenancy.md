# 14. Multi-Tenancy / Multi-Agency Isolation

## Status

Proposed (Phase 2). Requires a product decision on which deployment profiles are in scope.

## Context

The platform is single-tenant: `tenant_id` is a reserved `ContextVar` always `None`. The target
is dozens of agencies across multiple states plus national/intelligence bodies, with strong
sovereignty and air-gap requirements. `security-architecture.md` recommends physical isolation by
default; `deployment-architecture.md` defines four profiles (state/local, central agency,
single-tenant enterprise, future SaaS).

## Decision

1. **Default = physical/deployment isolation per agency** (separate deployment + database + key
   material + storage) for high-assurance government tenants. Safest for sovereignty and
   air-gapped operation, and it composes with the module-extraction model. This is the primary
   supported model.
2. **For a future multi-tenant (SaaS) profile only: schema-per-tenant**, chosen over shared-table
   row-level `tenant_id` because it gives stronger isolation, per-tenant backup/export/DR, and
   aligns with the existing schema-per-module Postgres strategy. Row-Level Security is applied as
   defense-in-depth if a shared tier is ever introduced.
3. **Activate the reserved tenant context** end-to-end: every repository/query is tenant-scoped;
   keys (ADR-0009) and storage buckets (ADR-0008) are per-tenant; audit records the tenant.
4. **Cross-agency sharing is an explicit, audited, event-mediated flow** — never shared tables or
   implicit joins.

## Consequences

- Strongest isolation by default, matching the government threat model; per-tenant blast-radius
  containment.
- Schema-per-tenant scales to hundreds of tenants, not tens of thousands — a large SaaS tier
  would revisit this ADR.
- Significant plumbing (tenant context everywhere, per-tenant keys/buckets/backup); gated on the
  product decision about which profiles to support. Phase-2.
