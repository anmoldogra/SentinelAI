# 16. Frontend Technology Stack for `apps/web`

## Status

Proposed. Required by `frontend-architecture.md`'s header note and §48, which state the React +
React Query choice "should be recorded as an ADR before implementation begins" — this records it
and closes the surrounding tooling `frontend-architecture.md` §2 deliberately left open.

## Context

`system-design.md` §9 listed the frontend framework as an open question.
`frontend-architecture.md` resolved the *library* half (React + React Query) but explicitly left
the build tool, routing library, and styling implementation open, deferring them to this ADR.
`apps/web` was a placeholder README until now, so this is decided before code exists rather than
ratified after the fact.

Constraints that actually narrow the choice, rather than a generic evaluation:

- **Air-gapped deployments** (PRD §8, `frontend-architecture.md` §2) — the toolchain must install
  from a vendored/offline registry mirror and build without network access. This rules out
  anything requiring a hosted build service.
- **Static-asset deployment, no SSR in Phase 1** (§2) — there is no Node runtime in production,
  so a meta-framework's server half would be dead weight.
- **Low-bandwidth on-prem networks** (§2) — code-splitting and bundle analysis are load-bearing,
  so the build tool's splitting story matters more than its dev-server speed.
- **Design tokens are mandatory** (§18–19) — no component may hardcode a colour, spacing, or
  typography value; theming (light/dark/high-contrast) must be a token-layer change.

## Decision

1. **React + React Query (TanStack Query)** — as `frontend-architecture.md` already fixed. Server
   state lives *only* in the React Query cache (§9); it is never copied into a client store.
2. **Vite** as the build tool and dev server. Plain SPA, no meta-framework: it builds to static
   assets with no server runtime, supports fully offline installs from a registry mirror, and its
   Rollup-based production build gives the manual chunking §39–41 depend on.
3. **React Router** as the routing library — the routes in §4 are a conventional nested
   hierarchy with no SSR or data-loader requirements that would justify anything heavier.
4. **Tailwind CSS v4 as the *implementation* of the §19 design-token layer, not as a licence to
   hardcode.** Tokens are declared once as semantic CSS custom properties (`--color-surface`,
   `--color-classification-restricted`) and exposed to Tailwind via its CSS-first `@theme`;
   feature code references the semantic utility, never a raw palette value like `bg-red-500`.
   Theme switching therefore remains a token-layer edit, as §18 requires. Tailwind v4's CSS-first
   configuration means the token layer *is* the config, rather than a parallel JS object that can
   drift from it.
5. **Native `fetch`, not Axios**, behind the §11 client layer. §11 describes a *thin* wrapper whose
   job is envelope parsing and header conventions; `fetch` covers that without a dependency, and
   the wrapper is the abstraction seam anyway, so the transport can change without touching
   feature code.
6. **TypeScript in `strict` mode**, plus `noUncheckedIndexedAccess` and
   `exactOptionalPropertyTypes` — mirroring the backend's `mypy --strict` posture rather than
   settling for a laxer default on the other side of the API.
7. **ESLint (flat config) + Prettier**, mirroring `ruff check` + `ruff format`'s split of
   correctness from formatting.

## Consequences

- The whole toolchain is offline-installable and produces static assets, so the air-gapped and
  cloud profiles build identically.
- Tailwind is a real risk to §19 if used naively: a developer writing `text-red-500` bypasses the
  token layer entirely. The mitigation is that only semantic tokens are defined in `@theme`, so
  bypassing them is visible in review; a lint rule restricting raw palette utilities is the
  natural enforcement step when the design system lands (§21).
- No SSR means the initial-payload cost of §2 is real and must be paid down by code-splitting;
  this ADR does not change that trade-off, it inherits it.
- React Router and Vite are both replaceable without touching feature code (routing is confined
  to `app/`, building is external to source) — unlike the React/React Query choice, which is
  pervasive. That asymmetry is why those two are recorded here as lower-stakes decisions.
- Revisit if a real low-bandwidth deployment proves bundle mitigation insufficient — §2's
  documented escape hatch is SSR/hybrid rendering, which would reopen items 2 and 3.
