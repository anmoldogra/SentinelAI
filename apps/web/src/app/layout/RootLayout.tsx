import { NavLink, Outlet } from "react-router-dom";

/**
 * Root layout shell (frontend-architecture.md §5): persistent chrome around the routed view.
 * Deliberately minimal — the real navigation model (§8), command palette (§37) and role-aware
 * dashboard (§23) are their own increments.
 */

const NAV_LINK_BASE = "rounded px-3 py-2 text-sm font-medium transition-colors";

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
        <Outlet />
      </main>
    </div>
  );
}
