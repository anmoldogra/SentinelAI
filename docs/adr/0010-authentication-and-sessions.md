# 10. Authentication, Sessions, and Access Model

## Status

Proposed — **amended 2026-08-30** (amendments A1–A3 below). Depends on ADR-0009. Resolves
documentation contradiction **D1**.

**Implementation status at the time of amendment** (verified against the code, not assumed):
§1 and §2's schema are **built** — `platform.sessions` carries `token_lookup` (indexed,
non-unique) + argon2id `token_hash`, and `database-design.md` §3.1 was updated in the same change,
so D1 is closed. `POST /auth/login` works and `require_role` enforces RBAC for real. **Not built:**
`/auth/refresh`, `/auth/logout`, all MFA (no MFA columns exist on `platform.users`), all SSO
endpoints, and `case_members`. `DbCaseAccessChecker` is ownership-only and says so in its own
docstring. The console today authenticates solely through the `VITE_DEV_ACCESS_TOKEN` dev seam.

Note that the platform is **currently non-compliant with `security-architecture.md` §8**, which
makes MFA "mandatory for every role that can access evidence or case data — no exceptions, per
PRD SR-2". Closing that is the point of the increment this amendment scopes.

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
   **Amended by A1.**
4. **Functional RBAC + ABAC.** `require_role` + `require_case_access` become real. ABAC needs a
   **`case_members` table** (case_id, user_id, role, granted_by/at) — this ADR adds it to
   `case_management`'s schema (currently missing) so access is membership-based, not
   ownership-only. Update `database-design.md` §3.4. **Amended by A2 — moved out of this ADR.**
5. **Token handling:** never in `localStorage`/`sessionStorage` (security §35) — httpOnly,
   secure, same-site cookie or platform secure store. **Amended by A3 — the "or" is resolved.**

## Amendments (2026-08-30)

### A1 — SSO is deferred by sequencing, not excluded by profile

Amends §3.

**The offline-capable methods are what this ADR now commits to building: TOTP (required minimum),
WebAuthn/FIDO2 (preferred), and PIV/CAC (preferred for government/LE).** All three complete
without any egress — TOTP is a shared-secret computation, WebAuthn is a local authenticator
ceremony, and PIV/CAC validates against the organization's own PKI. They are exactly
`security-architecture.md` §8's accepted factors, in its stated order of preference. SMS/voice OTP
remains **not supported**, per §8, as policy rather than omission.

**SSO/OIDC/SAML is deferred out of the next increment, and is explicitly NOT scoped out of any
deployment profile.** The distinction matters and was nearly recorded backwards:

- `security-architecture.md` §7 already states OIDC/SAML federation is air-gap compatible **"yes,
  with an on-prem IdP"**, and describes PIV/CAC itself as an identity-provider integration against
  the organization's existing PKI. Air-gapped enclaves routinely run their own Keycloak/ADFS and
  their own CA.
- PRD **SR-2** *requires* IdP integration — "rather than only local accounts". An ADR that scoped
  SSO out of on-prem deployments would contradict a canonical requirement, which is a PRD change,
  not an ADR amendment.

What is genuinely unavailable under `air-gapped` and `classified` (the two zero-egress profiles in
`platform/config.py`'s `VALID_PROFILES`) is the **internet-reachable** IdP. So: federation in
those profiles is supported only against an **enclave-local** IdP, and **local accounts plus one
of the three factors above are the guaranteed-available authentication path with zero external
dependency** — matching §7's own "local accounts … for air-gapped deployments with no reachable
external IdP" row. `identity_provider_links` and `users.external_idp_subject` already exist in the
schema, so deferring SSO costs no migration later.

### A2 — `case_members` / ABAC moves to its own ADR

Amends §4. **§4's `case_members` table and membership-based ABAC are removed from this ADR** and
become **ADR-0017 (Case Membership and Case-Level Access)**, to be written before that work starts.

They are separable on every axis that matters: `case_members` lives in `case_management`'s schema,
not `platform`'s; it changes *authorization* while everything else here changes *authentication*;
and it has no dependency on ADR-0009 (key management), which the rest of this ADR does. Bundling
them is the main reason `modernization-roadmap.md` sizes Wave 3.1 as **XL** — split, both halves
are independently shippable and reviewable.

This ADR retains §4's RBAC half. `require_role` is already functional; nothing further is needed
for it. Until ADR-0017 lands, `require_case_access` stays **ownership-only** — a known, documented
limitation rather than an unnoticed gap: a case is reachable only by its `owning_user_id`, so
collaboration on a case is not yet expressible. `database-design.md` §3.4 is **not** modified by
this ADR; that update belongs to ADR-0017.

### A3 — Refresh token is an httpOnly cookie; access token stays in memory

Amends §5, which offered "httpOnly, secure, same-site cookie **or** platform secure store" and
left the choice open. **The cookie is chosen**, because the frontend has already been built
assuming it — `shared/api/client.ts` sends `credentials: "same-origin"` with the comment "the
refresh-token cookie is HttpOnly; it must ride along for the session to be renewable". Leaving the
"or" unresolved is how the two ends drift apart, and one end has already committed.

The split, stated so both ends agree:

| Token | Transport | Lifetime | Why |
|---|---|---|---|
| Access (bearer) | `Authorization: Bearer` header, held **in JavaScript memory only** | Short | Never in `localStorage`/`sessionStorage` (security §35), never in a cookie — so it is not attached automatically and cannot be replayed by a cross-site request |
| Refresh | `HttpOnly; Secure; SameSite=Strict` cookie, `Path` scoped to the refresh endpoint | Long, sliding (§2) | Unreadable to script, so an XSS that steals the in-memory access token still cannot mint new sessions past its short expiry |

**Why this shape is CSRF-safe without a separate CSRF token:** the cookie authorizes *only* token
refresh, never an API call. Every protected endpoint requires the `Authorization` header, which a
cross-site request cannot set. The worst a forged cross-site POST to the refresh endpoint achieves
is causing a rotation whose response the attacker cannot read — `SameSite=Strict` blocks even
that. **This property is load-bearing: it holds only for as long as no endpoint accepts the cookie
as authentication.** If one ever does, a CSRF token becomes mandatory in the same change.

`Secure` is set in every profile except `development`. The air-gapped profile is served over plain
HTTP on a LAN address in some deployments (`frontend-architecture.md` §2); where that is the case,
the cookie's confidentiality rests on network isolation rather than TLS, and that trade is the
deployment's to make explicitly — it is not a reason to weaken the default.

Refresh **rotates**: each successful refresh issues a new token and revokes its predecessor
(`revoked_at`), so a stolen refresh token is single-use and its reuse is detectable.

## Consequences

- Unblocks every protected endpoint; makes RBAC/ABAC enforcement real end-to-end.
- Two schema changes (`sessions.token_hash`, new `case_members`) that also **fix/lift two
  documented gaps** (D1 and the ABAC-has-no-membership gap).
- Adds MFA/SSO integration surface and session-store operational concerns.

### Consequences of the 2026-08-30 amendments

- **Scope of the next increment is now: session lifecycle + MFA.** `/auth/refresh`,
  `/auth/logout`, TOTP enrollment and `/auth/mfa/verify`, the `apps/web` auth feature, and
  retiring `VITE_DEV_ACCESS_TOKEN`. One migration, on `platform.users`, for MFA secret storage —
  which no document currently specifies, so `database-design.md` §3.1 must be extended in the
  same change rather than the columns being invented in code (`CLAUDE.md` rule 1).
- **Two gaps stay open, deliberately and visibly**, rather than being quietly carried: ABAC
  remains ownership-only until ADR-0017, and SSO remains unbuilt. Both are sequencing decisions
  with a named owner, not silent omissions.
- **The A3 cookie decision constrains the backend to match a frontend that already shipped.** That
  is the correct direction here — the client's assumption is the standard one and is already
  reviewed — but it is worth naming that the sequencing was backwards, and that the CSRF argument
  is contingent on no endpoint ever accepting the cookie as authentication.
- **PRD SR-2 and `security-architecture.md` §7–8 are unchanged by these amendments.** A1 was
  deliberately written as deferral rather than exclusion precisely so that no requirement has to
  move; if SSO were ever genuinely scoped out of a profile, SR-2 would have to change first.
