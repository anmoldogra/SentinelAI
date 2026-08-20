# apps/web

The investigator-facing web console. **`docs/frontend-architecture.md` is the authoritative
reference**; the stack is recorded in `docs/adr/0016-frontend-technology-stack.md`.

React 19 · TypeScript (strict) · Vite · React Router · TanStack Query · Tailwind CSS v4

## Running it

```bash
cd apps/web
npm install
npm run dev          # http://localhost:5173
```

The backend must be running for API calls to resolve:

```bash
cd apps/server && make run-api    # http://localhost:8000
```

The dev server proxies `/api` to `http://localhost:8000`, so the browser sees one origin and the
app calls the same relative `/api/v1/...` paths in development and in production. Override the
target with `VITE_API_PROXY_TARGET` if the backend runs elsewhere.

There is no `make` target for the frontend: the repository's only Makefile (`apps/server`) is
backend-scoped, and adding a Node target to it would blur that boundary. The npm scripts below
are the entry points.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server with HMR and the `/api` proxy |
| `npm run build` | Type-check, then build static assets to `dist/` |
| `npm run preview` | Serve the built `dist/` locally |
| `npm run typecheck` | `tsc --build --force` — no emit, types only |
| `npm run lint` | ESLint (correctness; formatting is Prettier's job) |
| `npm run format` / `format:check` | Prettier write / verify |
| `npm run check` | format:check + lint + typecheck — the local gate, mirroring the backend's `make check` |

## Structure

Organised **by feature, not by technical layer** (`frontend-architecture.md` §3), deliberately
mirroring `apps/server/modules/*` — there are intentionally no top-level `components/`,
`hooks/`, or `services/` folders.

```
src/
  app/         routing, providers, root layout
  shared/      cross-feature pieces (API client, auth token store)
  features/
    cases/     mirrors the case_management module
```

Features never import from one another. The sanctioned cross-feature paths are a shared React
Query cache entry or a route link — never a direct component or hook import.

## Conventions that are load-bearing

- **The API client is the only place HTTP conventions live** (§11). Feature code never sets
  `Authorization`, `X-Correlation-Id`, or `Idempotency-Key` by hand.
- **Session state is in-memory only** — never `localStorage` or `sessionStorage`
  (`security-architecture.md` §35). ESLint fails the build if either is referenced.
- **Server state lives only in the React Query cache** (§9); it is never mirrored into a second
  store.
- **Nothing hardcodes a colour, spacing, or typography value** — everything goes through the
  semantic design tokens in `src/index.css` (§18–19), which is what makes theming a token-layer
  edit.
- **Review mutations are never optimistic** (§9). A finding's disposition changes on screen only
  after the server confirms it, because the human-in-the-loop guarantee (PRD FR-7.3) has to be
  visible in the UI's behaviour, not merely true on the backend.

## Status

Scaffolding only: routing, layout, the token layer, and the API client foundation. The Case
Dashboard renders no server data yet — feature surfaces (§23–29) are later increments.
