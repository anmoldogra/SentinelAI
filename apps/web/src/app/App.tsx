import { Navigate, Route, BrowserRouter as Router, Routes } from "react-router-dom";

import { CaseDashboardPage } from "@/features/cases/routes/CaseDashboardPage";

import { RootLayout } from "./layout/RootLayout";
import { AppProviders } from "./providers";

/**
 * Application root (frontend-architecture.md §4's route hierarchy, in miniature).
 *
 * Routes are declared here in `app/` rather than inside features, so no feature imports another
 * to navigate (§3). Feature routes are lazy-loaded as they land (§40); with one placeholder
 * screen there is nothing to split yet.
 */
export function App() {
  return (
    <AppProviders>
      <Router>
        <Routes>
          <Route element={<RootLayout />}>
            <Route index element={<Navigate to="/cases" replace />} />
            <Route path="/cases" element={<CaseDashboardPage />} />
            <Route path="*" element={<Navigate to="/cases" replace />} />
          </Route>
        </Routes>
      </Router>
    </AppProviders>
  );
}
