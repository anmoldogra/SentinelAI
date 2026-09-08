# 16. Frontend Technology Stack for `apps/web`

## Status

Accepted. Required by `frontend-architecture.md`'s header note and §48, which state the React +
React Query choice "should be recorded as an ADR before implementation begins" — this records it
and closes the surrounding tooling `frontend-architecture.md` §2 deliberately left open.

Moved from Proposed to Accepted once every item below was implemented in `apps/web` and gated in
CI (`.github/workflows/ci.yml`'s frontend job runs the Prettier, ESLint, and `tsc` checks item 6
and 7 describe). Items 4a and 4b were added at the same time: the token *mechanism* was decided
here from the start, but the palette semantics and typography rules it carries were only written
down in `frontend-architecture.md` §19.1, leaving the rule that `index.css` cites this ADR for
without an ADR actually stating it.

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

   Items 4a and 4b below fix what the roles *mean*. They are lettered rather than numbered so the
   existing citations to items 5–7 (`tsconfig.app.json` cites §6, `eslint.config.js` cites §7)
   keep resolving; renumbering this list silently breaks every reference into it.

   4a. **A tactical OSINT / SOC console aesthetic, dark-first.** Analysts read dense evidentiary
   data for hours, so the surface recedes and saturated colour is spent only where it carries
   meaning. Surfaces are slate/zinc in three depths (`canvas` → `surface` → `surface-raised`);
   three steps, not two, are what let a multi-panel workspace separate regions with borders
   instead of drawing a box around everything. **Cyan is reserved exclusively for interactive
   affordance** — focus rings, primary actions, links — and is never a status colour, so cyan
   always means "you can act on this". Status is carried by emerald (active/healthy), amber
   (attention/degraded), rose (failure and destructive intent), and neutral slate (terminal but
   unremarkable). Amber and rose are deliberately not interchangeable: conflating "needs
   attention" with "something broke" is the one ambiguity an operations console cannot afford.
   The modal backdrop is itself a token (`scrim`), not a hardcoded overlay. Dark is the *primary*
   look, not the only one — §18's light and high-contrast themes stay first-class, and every role
   resolves in each theme.

   4b. **Monospace is a semantic role, not a stylistic preference.** It marks content whose
   *character-level* detail is load-bearing: evidentiary identifiers, hashes, `payload_ref`s,
   pagination cursors, timestamps, durations, metadata keys, and enum/status values — an analyst
   comparing two SHA-256 digests or transcribing a case ID needs column alignment and unambiguous
   `0`/`O` and `1`/`l` glyphs. It **also** covers labels and panel headers that *name* an API
   field or resource, and command labels in a modal action row or panel toolbar, since those name
   machine-side identifiers rather than reading as prose. Everything read as language stays
   proportional: titles, descriptions, help text, validation messages, and the values an analyst
   types. The face is the platform's own monospace stack rather than a bundled webfont — the
   air-gapped profile cannot fetch one, and a font that silently failed to load would take the
   glyph-disambiguation guarantee with it.
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
- **4a and 4b are currently convention, not enforcement.** Nothing mechanically stops a component
  from using cyan for a status or monospacing a paragraph of prose; review is the only gate. This
  is the same exposure as the raw-palette risk above and has the same fix — the lint rule that
  restricts palette utilities should also be the place role misuse gets caught.
- A **precondition for** §36's WCAG 2.1 AA target, not a guarantee of it. Defining status as a
  role rather than a colour is what makes "colour is never the only signal" implementable — the
  status badge pairs the role with a text label — and routing every surface/text pair through
  tokens is what makes contrast auditable in one place. Neither property holds automatically:
  contrast ratios and the colour-plus-text rule still have to be verified per component.
- Naming colour families in an ADR is a deliberate, bounded exception to §19's "roles, never
  values" rule. `frontend-architecture.md` §19.1 and this item are the only places families are
  named; the concrete values live solely in `apps/web/src/index.css`, and component code still
  reaches for the role. Stating the family here is what stops "which green?" being re-litigated
  per feature.
- 4b's rule has one genuine collision — a form label that names an API field is both "a label"
  and "a field name". It resolves as monospaced, and `frontend-architecture.md` §17 and §19.1
  resolve it identically; that consistency is load-bearing, so a change to it belongs in all
  three places at once.
- No SSR means the initial-payload cost of §2 is real and must be paid down by code-splitting;
  this ADR does not change that trade-off, it inherits it.
- React Router and Vite are both replaceable without touching feature code (routing is confined
  to `app/`, building is external to source) — unlike the React/React Query choice, which is
  pervasive. That asymmetry is why those two are recorded here as lower-stakes decisions.
- Revisit if a real low-bandwidth deployment proves bundle mitigation insufficient — §2's
  documented escape hatch is SSR/hybrid rendering, which would reopen items 2 and 3.
