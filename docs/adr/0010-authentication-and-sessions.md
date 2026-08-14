# 10. Authentication, Sessions, and Access Model

## Status

Proposed. Depends on ADR-0009. Resolves documentation contradiction **D1**.

## Context

Authentication is a stub (`platform/auth/repository.py:get_active_by_token` raises), and the
schema makes it unbuildable as documented: `api-design.md` §Auth says the bearer token is
"backed by `platform.sessions`," but `database-design.md` §3.1 `sessions` has **no token
column** (contradiction D1). Every protected route is inert. `require_case_access` (ABAC) is
currently satisfiable only by ownership because there is **no case-membership table**
(`database-design.md` §3.4 has `cases.owning_user_id` only).

## Decision

1. **Resolve D1:** add `token_hash` (+ a short `token_lookup` prefix index) to
   `platform.sessions`; the bearer token is a high-entropy opaque secret, and the DB stores
   only its hash (argon2id / keyed HMAC via ADR-0009). Update `database-design.md` §3.1 in the
   same change.
2. **Opaque server-side sessions, not stateless JWT** — immediate revocation is a hard
   requirement (logout, compromise): `revoked_at`, sliding expiry via refresh, server-side
   invalidation.
3. **Login + MFA + SSO:** password (argon2id) → optional MFA (TOTP / WebAuthn / PIV-CAC per
   security §) via `mfa_token` exchange; SSO/OIDC via `identity_provider_links`.
4. **Functional RBAC + ABAC.** `require_role` + `require_case_access` become real. ABAC needs a
   **`case_members` table** (case_id, user_id, role, granted_by/at) — this ADR adds it to
   `case_management`'s schema (currently missing) so access is membership-based, not
   ownership-only. Update `database-design.md` §3.4.
5. **Token handling:** never in `localStorage`/`sessionStorage` (security §35) — httpOnly,
   secure, same-site cookie or platform secure store.

## Consequences

- Unblocks every protected endpoint; makes RBAC/ABAC enforcement real end-to-end.
- Two schema changes (`sessions.token_hash`, new `case_members`) that also **fix/lift two
  documented gaps** (D1 and the ABAC-has-no-membership gap).
- Adds MFA/SSO integration surface and session-store operational concerns.
