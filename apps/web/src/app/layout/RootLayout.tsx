import { Suspense } from "react";
import { NavLink, Outlet } from "react-router-dom";

/**
 * Root layout shell (frontend-architecture.md §5): persistent chrome around the routed view.
 * Deliberately minimal — the real navigation model (§8), command palette (§37) and role-aware
 * dashboard (§23) are their own increments.
 */

const NAV_LINK_BASE = "rounded px-3 py-2 text-sm font-medium transition-colors";

/**
 * Fallback while a lazily-loaded route chunk is in flight (§40).
 *
 * Deliberately plain: a route chunk on a local network resolves in milliseconds, so anything
 * elaborate would flash rather than inform. It reserves a little height so the shell does not
 * collapse and then jump when the module lands, and it is a live region — a sighted user sees the
 * indicator, and without `role="status"` a screen-reader user would get silence between activating
 * a link and the new screen arriving (§36).
 */
function ModuleLoader() {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex min-h-48 items-center justify-center font-mono text-xs uppercase tracking-wider text-text-muted"
    >
      <span className="animate-pulse">Loading module…</span>
    </div>
  );
}

export function RootLayout() {
  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
          <span className="text-base font-semibold tracking-tight">SentinelAI</span>
          <nav aria-label="Primary">
            <NavLink
              to="/cases"
              className={({ isActive }) =>
                isActive
                  ? `${NAV_LINK_BASE} bg-accent text-accent-contrast`
                  : `${NAV_LINK_BASE} text-text-muted hover:text-text`
              }
            >
              Cases
            </NavLink>
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-7xl flex-1 px-6 py-8">
        {/*
         * The boundary sits *inside* the layout, wrapping only the outlet — not around the router
         * in `App.tsx`. That placement is the whole point: a boundary above the layout would
         * unmount the header and primary navigation on every chunk fetch, so the application
         * would appear to blink out and rebuild itself each time an analyst opened a new section.
         * Here the shell stays put and only the routed region is replaced.
         */}
        <Suspense fallback={<ModuleLoader />}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
