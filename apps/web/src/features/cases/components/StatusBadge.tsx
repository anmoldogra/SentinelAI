/**
 * Case lifecycle status badge.
 *
 * Lifted out of `CaseDashboardPage` when the detail route became a second consumer: the status
 * token map has to exist in exactly one place, or the exhaustiveness check below stops being a
 * guarantee the moment the two copies drift.
 */

import type { CaseStatus } from "../types";

/**
 * Keyed by `CaseStatus`, so adding a lifecycle state to the union fails the build here until it
 * has a token — the styles cannot silently fall out of step with the domain.
 */
const STATUS_STYLES: Record<CaseStatus, string> = {
  open: "bg-status-open/15 text-status-open",
  closed: "bg-status-closed/15 text-status-closed",
  archived: "bg-status-archived/15 text-status-archived",
};

function statusStyle(status: string): string {
  // An unrecognised status (a newer server) degrades to neutral rather than throwing.
  return status in STATUS_STYLES
    ? STATUS_STYLES[status as CaseStatus]
    : "bg-surface text-text-muted";
}

export function StatusBadge({ status }: { status: string }) {
  // §36: colour is never the only signal — the label is always present, so the badge still reads
  // correctly in high-contrast mode or to a colour-blind analyst.
  const style = statusStyle(status);
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${style}`}
    >
      {status}
    </span>
  );
}
