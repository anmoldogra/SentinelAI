import { Navigate, Route, BrowserRouter as Router, Routes } from "react-router-dom";

import { DashboardPage } from "@/features/dashboard/routes/DashboardPage";
import { CaseEvidencePage } from "@/features/cases/routes/CaseEvidencePage";
import { CaseFindingsPage } from "@/features/cases/routes/CaseFindingsPage";
import { CaseLayout } from "@/features/cases/routes/CaseLayout";
import { CaseListPage } from "@/features/cases/routes/CaseListPage";
import { CaseOverviewPage } from "@/features/cases/routes/CaseOverviewPage";
import { CaseReportsPage } from "@/features/cases/routes/CaseReportsPage";
import { CaseTimelinePage } from "@/features/cases/routes/CaseTimelinePage";
import { EntityGraphPage } from "@/features/investigation/routes/EntityGraphPage";

import { AppLayout } from "./layout/AppLayout";
import { AppProviders } from "./providers";

/**
 * Application root (frontend-architecture.md §4's route hierarchy). Routes are declared here in
 * `app/` rather than inside features, so no feature imports another to navigate (§3).
 *
 * `/cases/:caseId/graph` and `/cases/:caseId/reports` are deliberately NOT nested under
 * `CaseLayout` — the approved design gives them their own chrome (no tab strip, no persistent
 * copilot aside), unlike Overview/Evidence/Findings/Timeline, which share both.
 */
export function App() {
  return (
    <AppProviders>
      <Router>
        <Routes>
          <Route element={<AppLayout />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/cases" element={<CaseListPage />} />
            <Route path="/cases/:caseId" element={<CaseLayout />}>
              <Route index element={<CaseOverviewPage />} />
              <Route path="evidence" element={<CaseEvidencePage />} />
              <Route path="findings" element={<CaseFindingsPage />} />
              <Route path="timeline" element={<CaseTimelinePage />} />
            </Route>
            <Route path="/cases/:caseId/graph" element={<EntityGraphPage />} />
            <Route path="/cases/:caseId/reports" element={<CaseReportsPage />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Route>
        </Routes>
      </Router>
    </AppProviders>
  );
}
