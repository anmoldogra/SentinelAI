/**
 * Placeholder Case Dashboard (frontend-architecture.md §25 is the real specification).
 *
 * Intentionally static: it renders no server data because the case-list query hook, its loading
 * and error surfaces (§12-13), and the table/filter patterns (§30-31) are separate increments.
 * It exists to prove routing, layout, and the token layer render end to end.
 */
export function CaseDashboardPage() {
  return (
    <section aria-labelledby="cases-heading" className="space-y-4">
      <h1 id="cases-heading" className="text-2xl font-semibold tracking-tight">
        Cases
      </h1>
      <p className="max-w-prose text-sm text-text-muted">
        Scaffolding only. The case list, filters, and detail view are built against
        <code className="mx-1 rounded bg-surface px-1 py-0.5">GET /api/v1/cases</code>
        in a later increment.
      </p>
      <div className="rounded-lg border border-border bg-surface p-6">
        <p className="text-sm text-text-muted">No cases loaded.</p>
      </div>
    </section>
  );
}
