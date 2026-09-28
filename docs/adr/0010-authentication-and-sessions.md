# 10. Authentication, Sessions, and Access Model

## Status

**Accepted — Built**, except SSO. Amended 2026-08-30 (A1–A3 below); the increment those
amendments scoped — session lifecycle + MFA — landed in modernization **Wave 3.1**. Depends on
ADR-0009. Resolves documentation contradiction **D1**.

| Decision | State |
|---|---|
| §1 D1 resolution: `sessions.token_hash` + `token_lookup` | **Built** (pre-Wave-3.1) — argon2id digest, non-unique prefix index; `database-design.md` §3.1 updated in the same change |
| §2 Opaque server-side sessions, immediate revocation, sliding expiry | **Built** — `POST /auth/refresh` rotates (successor issued, predecessor `revoked_at`), `POST /auth/logout` revokes on demand |
| §3 Password login | **Built** (pre-Wave-3.1) — argon2id, uniform rejection, uniform timing |
| §3 MFA (TOTP + recovery codes), per A1 | **Built** — storage landed earlier; Wave 3.1 made login **consult** it. An enrolled account now receives an `mfa_token`, never a session, until `POST /auth/mfa/verify` succeeds |
| §3 MFA enrolment path | **Built** — `python -m sentinelai.cli.admin enroll-mfa`. No *self-service* endpoint: `api-design.md` §9 documents none, and one has to be specified before it is built |
| §3 SSO / OIDC / SAML | **Not built** — deferred by sequencing (A1), not excluded by profile. `identity_provider_links` and `users.external_idp_subject` already exist, so it costs no migration |
| §4 RBAC (`require_role`) | **Built** (pre-Wave-3.1) |
| §4 ABAC / `case_members` | **Moved out** by A2 → **ADR-0017**, which is now Accepted and Built. `require_case_access` is owner-or-member and audits its denials |
| §5 / A3 Access token in memory, never web storage | **Built** — `apps/web/src/shared/auth/token-store.ts` has no storage path and ESLint bans the globals |
| §5 / A3 Refresh credential as an `HttpOnly` cookie | **Not built** — `POST /auth/refresh` takes the token in the request body. See the note |

**`security-architecture.md` §8 compliance is now enforced rather than merely intended.** The
non-compliance this status block previously recorded — MFA storage that nothing consulted — is
closed: a password alone cannot open a session for an enrolled account, and
`tests/integration/test_session_lifecycle_db.py` is the proof.

## Implementation note (2026-09-29, Wave 3.1)

### MFA was storage without enforcement, which is worse than no MFA

The columns, the `mfa_challenges` table, the recovery codes, the `MfaRepository` and a
vector-tested RFC 6238 implementation all existed before this wave. Nothing read them:
`AuthService.login` issued a session on a correct password regardless of `mfa_enrolled_at`. An
account could complete enrolment, believe it had a second factor, and be protected by one factor —
the failure mode a user cannot detect and would not expect. Wave 3.1 added the branch.

Two ordering decisions inside the exchange are security-relevant:

**The challenge is consumed before the code is checked.** Consuming only on success would let an
attacker holding a stolen `mfa_token` try six digits repeatedly until it expired. Consuming first
makes each attempt cost a fresh password login.

**The account's status is re-checked at the second factor**, not only at the password. The window
between the two steps is small, but an account disabled inside it must not be able to walk through.

### Enforcement without an enrolment path would have been dead code

Nothing in the codebase called `MfaRepository.store_secret`. Adding the login branch alone would
have produced a feature that never fires: no account could reach `mfa_enrolled_at`, so §8's
"mandatory" factor would have remained unmet in practice while the code claimed otherwise — the
worst of both, because it reads as done.

`enroll-mfa` is the provisioning path, for the same reason `create-user` is: `POST /auth/login` could
not be used until something could create a user, and `api-design.md` documents no admin-user
endpoint either. It generates the secret, prints the provisioning URI and ten recovery codes **once**
(neither is recoverable from the database afterwards), and audits the enrolment.

It is deliberately **not idempotent** — re-running mints a new secret and invalidates the
authenticator the user already registered — so it refuses an enrolled account unless `--replace`
states that intent. And unlike `dev-token` it carries **no** production restriction: refusing to
enrol a real operator in production would make the mandatory factor unprovisionable exactly where it
matters most.

Recovery codes use a Crockford-style base32 alphabet with the confusable characters removed
(`I`, `L`, `O`, `U`, `0`, `1`). A code is read off a screen and typed back, and an alphabet holding
both `0` and `O` guarantees support tickets. Ten characters is ~51 bits — less than a session token,
deliberately: a longer code gets transcribed wrong, and each attempt already costs an argon2id verify
and is single-use.

### Refresh is rotation, and the cookie is not built

A3 specifies a two-token scheme: a short-lived access token in memory and a long-lived refresh
credential in an `HttpOnly; Secure; SameSite=Strict` cookie scoped to the refresh endpoint. What
Wave 3.1 built is the **rotation** half — `POST /auth/refresh` issues a successor session and sets
`revoked_at` on its predecessor, so a stolen token is single-use and its reuse is detectable, which
is A3's stated security property.

The **transport** half is not built: there is one credential, not two, and it travels in the
request body. Completing A3 means a second token class (a schema change on `platform.sessions`),
cookie issuance, and a matching change in `apps/web` — a coordinated front-and-back increment
rather than a detail of this one.

A3's CSRF argument is unaffected and still holds, for the same reason it did before: **no endpoint
accepts a cookie as authentication.** That property remains load-bearing, and the day one does, a
CSRF token becomes mandatory in the same change.

### Logout takes the header directly, not `get_current_user`

`get_current_user` rejects an expired or already-revoked session with a `401`, and logout has to
succeed for exactly those. A caller told their logout failed will reasonably conclude the session
is still live. It returns `204` whether or not anything was revoked, so the response cannot tell
the holder of a stale token whether it was ever real.

### The developer seams were already restricted — and are now tested

Both bypass paths turn on `Settings.is_production`, which deliberately spans three profiles
(`production`, `air-gapped`, `classified`) because the latter two are hardening overlays on the
first. `issue_dev_token` refuses outright on any of them, and `apps/web`'s `VITE_DEV_ACCESS_TOKEN`
sits behind `import.meta.env.DEV` so it is dead-code eliminated from a production build — and it
never manufactures a session, since the server still resolves the token against a real
`platform.sessions` row.

Neither guard had a test. `tests/unit/test_dev_seam_guards.py` adds them, including one that fails
if a sixth profile is ever added without being classified as production-grade or not — the shape of
mistake (`app_env == "production"`) that would leave the two most sensitive deployments open.

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
