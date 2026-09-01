import { useEffect, useState } from "react";
import { matchPath, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { CommandPalette } from "@/shared/console/CommandPalette";
import "@/features/console/styles/console.css";

const DEFAULT_CASE_ID = "CASE-2041";

/**
 * Authenticated app shell (frontend-architecture.md §5's `AppLayout`): persistent header with
 * global nav, search/⌘K trigger, notifications, and user identity; routed content renders below
 * via `Outlet`. The console screens (dashboard/cases/investigation) are the shell's only consumers
 * today, so the Nocturne token layer (features/console/styles/console.css) is scoped to `.sai-shell`
 * here rather than promoted into the app-wide token layer — see that file's header comment.
 */
export function AppLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  const [lastCaseId, setLastCaseId] = useState(DEFAULT_CASE_ID);

  const caseMatch = matchPath("/cases/:caseId/*", location.pathname);
  const activeCaseId = caseMatch?.params["caseId"];

  useEffect(() => {
    if (activeCaseId) {
      setLastCaseId(activeCaseId);
    }
  }, [activeCaseId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        setPaletteQuery("");
      } else if (e.key === "Escape") {
        setPaletteOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  const currentCaseId = activeCaseId ?? lastCaseId;
  const isGraph = /^\/cases\/[^/]+\/graph/.exec(location.pathname) !== null;
  const isReports = /^\/cases\/[^/]+\/reports/.exec(location.pathname) !== null;
  const isCases = location.pathname.startsWith("/cases") && !isGraph && !isReports;

  return (
    <div
      className="sai-shell"
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        fontFamily: "var(--sc-font-body)",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16.8,
          padding: "11.2px 22.4px",
          boxShadow: "inset 0 -1px 0 var(--sc-divider)",
          position: "sticky",
          top: 0,
          background: "var(--sc-bg)",
          zIndex: 5,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8.4, marginRight: 11.2 }}>
          <i
            className="ph-fill ph-shield-check"
            style={{ fontSize: 18, color: "var(--sc-accent)" }}
          />
          <span
            style={{
              fontFamily: "var(--sc-font-heading)",
              fontWeight: 500,
              fontSize: 16,
              letterSpacing: "-0.015em",
            }}
          >
            SentinelAI
          </span>
        </div>
        <nav aria-label="Primary" style={{ display: "flex", gap: 2.8 }}>
          <NavLink
            to="/dashboard"
            className="sc-nav-link"
            aria-current={location.pathname === "/dashboard" ? "page" : undefined}
          >
            Dashboard
          </NavLink>
          <NavLink
            to={`/cases/${currentCaseId}`}
            className="sc-nav-link"
            aria-current={isCases ? "page" : undefined}
          >
            Cases
          </NavLink>
          <button
            type="button"
            className="sc-nav-link"
            onClick={() => {
              void navigate(`/cases/${currentCaseId}/evidence`);
            }}
          >
            Evidence
          </button>
          <NavLink
            to={`/cases/${currentCaseId}/graph`}
            className="sc-nav-link"
            aria-current={isGraph ? "page" : undefined}
          >
            Graph
          </NavLink>
          <NavLink
            to={`/cases/${currentCaseId}/reports`}
            className="sc-nav-link"
            aria-current={isReports ? "page" : undefined}
          >
            Reports
          </NavLink>
        </nav>
        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8.4 }}>
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => {
              setPaletteOpen(true);
              setPaletteQuery("");
            }}
            style={{ color: "color-mix(in srgb, var(--sc-text) 60%, transparent)", gap: 8.4 }}
          >
            <i className="ph ph-magnifying-glass" style={{ fontSize: 15 }} /> Search evidence,
            entities, cases{" "}
            <span className="sc-code" style={{ opacity: 0.7 }}>
              ⌘K
            </span>
          </button>
          <button type="button" className="btn btn-secondary btn-icon" aria-label="Notifications">
            <i className="ph ph-bell" style={{ fontSize: 16 }} />
          </button>
          <span className="tag tag-neutral">r.okafor · IR analyst</span>
        </div>
      </header>

      <Outlet />

      <CommandPalette
        open={paletteOpen}
        query={paletteQuery}
        onQueryChange={setPaletteQuery}
        onClose={() => {
          setPaletteOpen(false);
        }}
        currentCaseId={currentCaseId}
      />
    </div>
  );
}
