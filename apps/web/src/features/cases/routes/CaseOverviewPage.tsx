/**
 * Case workspace — Overview tab (`/cases/:caseId`, frontend-architecture.md §25).
 *
 * Extracted from `CaseDetailPage` when that became the tab layout. This owns §25.1's *operations
 * grid* and the reserved graph panel; the command header stays in the layout, because it is
 * persistent chrome across every tab rather than Overview's content.
 *
 * Re-reads the case through `useCase` rather than taking it via props or `Outlet` context. That
 * is not a second request: the layout has already populated `casesQueryKeys.detail(caseId)`, and
 * §9 puts server state solely in the React Query cache, so reading it from the cache is how a tab
 * gets the case — copying it down through the tree would create the second source of truth §9
 * exists to prevent.
 */

import { useParams } from "react-router-dom";

import { useCase } from "../api/useCase";
import { StatusBadge } from "../components/StatusBadge";
import { formatDateTime } from "../format";
import type { Case } from "../types";

/** §25.1: panels are `surface` regions separated by borders, not by whitespace. */
const PANEL = "rounded-lg border border-border bg-surface";

/** §19.1's field-name role, shared by the operations grid and the panel headers. */
const PANEL_LABEL = "font-mono text-xs uppercase tracking-wider text-text-muted";

/**
 * One metadata cell in the operations grid. A `<dt>`/`<dd>` pair so the label-to-value
 * relationship is exposed to assistive technology, not merely implied by position (§25.1).
 *
 * `mono` marks values whose character-level content matters — identifiers and timestamps — per
 * §19.1; prose values leave it off.
 */
function Field({
  label,
  mono = false,
  className = "",
  children,
}: {
  label: string;
  mono?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`bg-surface px-6 py-4 ${className}`}>
      <dt className={PANEL_LABEL}>{label}</dt>
      <dd className={`mt-1.5 text-sm ${mono ? "break-all font-mono text-xs" : ""}`}>{children}</dd>
    </div>
  );
}

/** Rendered where a value is absent, so an empty cell never reads as a failed load. */
function Absent({ children }: { children: React.ReactNode }) {
  return <span className="font-normal text-text-muted">{children}</span>;
}

/**
 * The reserved graph region (§25.1, §27).
 *
 * Its area is claimed *before* any rendering library exists: a force simulation handed a
 * container that resizes on every parent reflow re-runs its layout and visibly jitters, so the
 * minimum height and bounded scroll context are established now and the simulation will inherit a
 * stable box rather than force a layout change later. The empty state names what will occupy the
 * panel and its data source, so a reserved region never reads as a silently failed load.
 *
 * It sits on Overview rather than behind its own tab because §25's Graph tab is a later
 * increment; when that lands, this panel moves to `/cases/:caseId/graph` intact.
 */
function EntityGraphPanel() {
  return (
    <section aria-labelledby="graph-panel-heading" className={`${PANEL} overflow-hidden`}>
      <div className="border-b border-border px-6 py-3">
        <h2 id="graph-panel-heading" className={PANEL_LABEL}>
          Entity relationship graph
        </h2>
      </div>
      <div className="flex min-h-96 items-center justify-center overflow-auto px-6 py-10">
        <div className="max-w-prose text-center">
          <p className="text-sm font-medium text-text">Graph rendering is not wired up yet.</p>
          <p className="mt-1 text-sm text-text-muted">
            This panel is reserved for the force-directed entity graph — nodes styled by{" "}
            <span className="font-mono text-xs">entity_type</span>, edges by type, confidence, and
            review status, with proposed edges visually distinct from confirmed ones.
          </p>
          <p className="mt-3 font-mono text-xs text-text-muted">GET /cases/{"{case_id}"}/graph</p>
        </div>
      </div>
    </section>
  );
}

function OperationsGrid({ item }: { item: Case }) {
  return (
    // `gap-px` over the border colour draws the hairlines between cells, which is what keeps a
    // dense metadata region legible without a border on every cell.
    <dl className={`${PANEL} grid gap-px overflow-hidden bg-border sm:grid-cols-2 lg:grid-cols-3`}>
      <Field label="Status">
        <StatusBadge status={item.status} />
      </Field>
      <Field label="Created" mono>
        {formatDateTime(item.created_at)}
      </Field>
      <Field label="Closed" mono>
        {item.closed_at === null ? <Absent>Still open</Absent> : formatDateTime(item.closed_at)}
      </Field>
      <Field label="Case ID" mono>
        {item.case_id}
      </Field>
      <Field label="Owning user" mono>
        {item.owning_user_id}
      </Field>
      <Field label="Description" className="sm:col-span-2 lg:col-span-3">
        {item.description ?? <Absent>No description</Absent>}
      </Field>
    </dl>
  );
}

export function CaseOverviewPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const { data } = useCase(caseId);

  // The layout renders the skeleton and the error state before it renders this outlet, so the
  // only way to arrive here without data is a cache eviction mid-render. Rendering nothing is
  // correct for that instant — the layout's own states own every other case.
  if (data === undefined) {
    return null;
  }

  return (
    <div className="space-y-4">
      <OperationsGrid item={data} />
      <EntityGraphPanel />
    </div>
  );
}
