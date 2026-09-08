import { lazy } from "react";
import { Navigate, Route, BrowserRouter as Router, Routes } from "react-router-dom";

import { RootLayout } from "./layout/RootLayout";
import { AppProviders } from "./providers";

/**
 * Application root (frontend-architecture.md §4's route hierarchy, in miniature).
 *
 * Routes are declared here in `app/` rather than inside features, so no feature imports another
 * to navigate (§3).
 *
 * **Every feature route is a separate chunk (§40, §41).** Vite splits on `import()` automatically,
 * so each route below becomes its own file that the browser fetches only when an analyst actually
 * navigates there. That matters concretely rather than theoretically here: the evidence feature now
 * carries a detail page, a custody ledger, and a Web Crypto chain-verification module, none of
 * which a user looking at the case list has any reason to download. §2 calls the SPA payload a real
 * cost on low-bandwidth on-prem networks, and §39–41 treat splitting as a requirement rather than
 * polish.
 *
 * `RootLayout` and the providers stay eager — they are the shell that renders *while* a chunk
 * loads, so deferring them would defeat the point.
 *
 * **The `.then()` mapping is not ceremony.** `lazy` requires a module whose `default` is the
 * component, and every route component in this codebase is a named export. Mapping the name here
 * keeps that convention intact rather than adding default exports to five feature files purely to
 * satisfy the loader — and it keeps the whole change confined to this file.
 */

const CaseDashboardPage = lazy(() =>
  import("@/features/cases/routes/CaseDashboardPage").then((module) => ({
    default: module.CaseDashboardPage,
  })),
);

const CaseDetailPage = lazy(() =>
  import("@/features/cases/routes/CaseDetailPage").then((module) => ({
    default: module.CaseDetailPage,
  })),
);

const CaseOverviewPage = lazy(() =>
  import("@/features/cases/routes/CaseOverviewPage").then((module) => ({
    default: module.CaseOverviewPage,
  })),
);

const CaseEvidencePage = lazy(() =>
  import("@/features/cases/routes/CaseEvidencePage").then((module) => ({
    default: module.CaseEvidencePage,
  })),
);

const EvidenceDetailPage = lazy(() =>
  import("@/features/evidence/routes/EvidenceDetailPage").then((module) => ({
    default: module.EvidenceDetailPage,
  })),
);

export function App() {
  return (
    <AppProviders>
      <Router>
        <Routes>
          {/* The Suspense boundary lives inside `RootLayout`, around its `<Outlet/>`, so the
              application header stays mounted while a route chunk is in flight. */}
          <Route element={<RootLayout />}>
            <Route index element={<Navigate to="/cases" replace />} />
            <Route path="/cases" element={<CaseDashboardPage />} />
            {/*
             * The case workspace is a layout route (§25): `CaseDetailPage` renders the persistent
             * command header and tab bar, and each tab is a nested route rendered through its
             * `<Outlet/>`. Declared before the catch-all, which would otherwise swallow it.
             */}
            <Route path="/cases/:caseId" element={<CaseDetailPage />}>
              <Route index element={<CaseOverviewPage />} />
              <Route path="evidence" element={<CaseEvidencePage />} />
            </Route>
            {/*
             * Top-level, not nested under a case: an evidence item exists independently of any
             * single case and can be linked to several, so its URL must not imply one owner.
             */}
            <Route path="/evidence/:evidenceId" element={<EvidenceDetailPage />} />
            <Route path="*" element={<Navigate to="/cases" replace />} />
          </Route>
        </Routes>
      </Router>
    </AppProviders>
  );
}
