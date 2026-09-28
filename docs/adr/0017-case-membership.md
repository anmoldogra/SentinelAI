# 17. Case Membership and Case-Level Access

## Status

**Accepted — Built** in modernization Wave 3.1, alongside the session-lifecycle half of ADR-0010.

Created by **ADR-0010 amendment A2 (2026-08-30)**, which removed `case_members` and
membership-based ABAC from ADR-0010 and required this ADR to exist before the work started. They
are separable on every axis that matters: `case_members` lives in `case_management`'s schema, not
`platform`'s; it changes *authorization* while ADR-0010 changes *authentication*; and it has no
dependency on ADR-0009 (key management), which ADR-0010 does.

| Decision | State |
|---|---|
| §1 `case_members` table in the `case_management` schema | **Built** — `202609290007_case_members`; `database-design.md` §3.4 updated in the same change |
| §2 Access = owner **or** active member | **Built** — `DbCaseAccessChecker` is no longer ownership-only |
| §3 Membership grant/revoke API | **Built** — `GET`/`PUT`/`DELETE /api/v1/cases/{case_id}/members`; `api-design.md` §4.2 updated in the same change |
| §4 Every ABAC denial is audited | **Built** — `require_case_access` writes `case_access_denied` to `platform.audit_log` |
| §5 Membership is append-only history | **Not built** — see Consequences. Revocation is a row delete plus an audit entry, not a tombstone |

## Context

`security-architecture.md` §6 defines authorization as RBAC for the action class plus ABAC for the
specific resource, and names **"case-scope grant"** as the first ABAC attribute. §6's worked
example turns on it directly: an `investigator` requesting custody events for evidence "linked to a
case they are not assigned to" passes RBAC and must fail ABAC.

There was nothing to evaluate that against. `DbCaseAccessChecker` resolved `cases.owning_user_id`
and compared it to the caller, because `database-design.md` §3.4 had no membership table — so
"assigned to a case" was not expressible at all, and a case was reachable by exactly one person.
Every collaborative workflow in the PRD (a forensic examiner processing devices "for multiple case
teams", a supervisor reviewing an investigator's findings) was therefore unimplementable, and the
ABAC half of the authorization model was decorative.

A second, quieter gap: `platform/auth/dependencies.py`'s own module docstring said both checks "are
audited regardless of outcome", and neither was. §6 requires the denial specifically — "the denial
itself is written to `platform.audit_log` with the caller's identity, the resource requested, and
the reason" — because a compliance review has to be able to distinguish "this analyst never had
access" from "this analyst had access and used it".

## Decision

1. **`case_members` in the `case_management` schema**, with the columns ADR-0010 §4 specified
   before A2 moved them: `case_id`, `user_id`, `role`, `granted_by_user_id`, `granted_at`. The
   `case_id` is a real intra-schema foreign key; `user_id` and `granted_by_user_id` are unenforced
   `uuid` app-refs to `platform.users`, because `database-design.md` §5 forbids cross-schema
   foreign keys. `(case_id, user_id)` is the primary key — a user is a member of a case once, with
   one role.

2. **Access is owner OR active member.** The owner keeps implicit access and is not written into
   `case_members` at case creation: `owning_user_id` is already the authoritative fact, and
   duplicating it into a membership row creates two places that can disagree about who owns a case.
   One query answers both halves.

3. **Membership is granted through the API, by a user who already has case access.** Without a
   grant path the table stays empty and ABAC stays ownership-only in practice, so this ADR adds
   `GET`/`PUT`/`DELETE /api/v1/cases/{case_id}/members` and documents them in `api-design.md` §4.2
   in the same change. `PUT` is idempotent — re-granting an existing membership updates its role
   rather than colliding, which is what makes the endpoint safe to retry under §2.9.

4. **Every ABAC denial is audited**, as §6 requires, with the caller, the case, and the reason.
   Grants and revocations are audited too: membership *is* the access-control state, so a change to
   it is more security-relevant than most of what the audit log already records.

5. **The membership role is a label, not a second permission system.** It records *why* someone is
   on a case (`lead`, `investigator`, `analyst`, `observer`) for the audit trail and the UI.
   Authorization decisions read RBAC roles from `platform.user_roles`; nothing branches on the
   membership role. A per-case permission matrix layered under the platform's roles would be a
   second authorization model to keep consistent with the first, and §6 describes one.

## Consequences

- `require_case_access` becomes a real ABAC check, so §6's worked example now behaves as documented
  and collaboration on a case is expressible.
- Denials are recorded. That is a new class of audit-log write on a path that previously only
  rejected, so failed-access volume becomes a signal SR-11's misuse detection can consume.
- **Membership has no history.** A revocation deletes the row; the audit log records that it
  happened, but `case_members` itself cannot answer "who was on this case in March". That is a
  deliberate simplification — the audit log is already the append-only record of record, and the
  alternative (a `revoked_at` tombstone plus every query filtering on it) adds a nullable column to
  every access check to duplicate what the ledger already holds. If a future requirement needs
  point-in-time membership reconstruction from the table itself rather than from the audit log,
  that is a schema change with a migration, and it is recorded here as not built rather than
  assumed.
- **Access is binary per case.** Evidence classification vs. caller clearance, `legal_authority_ref`
  presence, and time-of-day context — the other ABAC attributes §6 lists — are not evaluated. This
  ADR closes the case-scope attribute only; the rest remain open, and `require_case_access` is the
  seam where they would go.
