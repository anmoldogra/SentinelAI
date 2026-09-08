/**
 * Case workspace layout (`/cases/:caseId`, frontend-architecture.md §25).
 *
 * A route rather than a modal, per §17: this is a view an analyst bookmarks, deep-links, and
 * sends to a supervisor, and all three require a URL. The tabs are real nested routes for the
 * same reason — a tab an analyst cannot link a supervisor to is not finished.
 *
 * This component owns only what is persistent across tabs: §25.1's command header, the tab bar,
 * and the fetch/skeleton/error states for the case itself. Each tab's content is its own route
 * component rendered through `<Outlet/>`, so a new tab is a new route rather than a new branch
 * here.
 *
 * Only Overview and Evidence exist today. §25's Graph, Timeline, and Reports tabs are separate
 * increments and are deliberately not rendered as disabled stubs — a tab that looks navigable and
 * is not is worse than an absent one.
 */

import { Link, NavLink, Outlet, useParams } from "react-router-dom";

import { describeApiError } from "@/shared/api/errors";

import { useCase } from "../api/useCase";
import { StatusBadge } from "../components/StatusBadge";
import { formatDateTime } from "../format";
import type { Case } from "../types";

/** §25.1: panels are `surface` regions separated by borders, not by whitespace. */
const PANEL = "rounded-lg border border-border bg-surface";

/** §19.1's field-name role. */
const PANEL_LABEL = "font-mono text-xs uppercase tracking-wider text-text-muted";

const TAB_BASE =
  "-mb-px border-b-2 px-4 py-2.5 font-mono text-xs uppercase tracking-wider transition-colors";

function BackToCases() {
  return (
    <Link to="/cases" className="text-sm text-text-muted hover:text-text hover:underline">
      ← Back to cases
    </Link>
  );
}

/** §13: a skeleton shaped like the eventual content, so the page does not reflow when it lands. */
function CaseDetailSkeleton() {
  return (
    <div className="space-y-4" aria-hidden="true">
      <div className={`${PANEL} space-y-3 px-6 py-4`}>
        <div className="h-3 w-64 animate-pulse rounded bg-border" />
        <div className="h-7 w-1/3 animate-pulse rounded bg-border" />
      </div>
      <div className={`${PANEL} grid gap-px sm:grid-cols-2 lg:grid-cols-3`}>
        {Array.from({ length: 6 }, (_, index) => (
          <div key={index} className="space-y-2 px-6 py-4">
            <div className="h-3 w-20 animate-pulse rounded bg-border" />
            <div className="h-4 w-32 animate-pulse rounded bg-border" />
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * §25.1's command header: the identity strip an analyst orients from. Pinned, so it survives
 * scrolling — the case ID and status stay readable while a tab's panels scroll past.
 */
function CommandHeader({ item }: { item: Case }) {
  return (
    <header className={`${PANEL} sticky top-0 z-10 px-6 py-4`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={PANEL_LABEL}>Case</span>
        <span className="break-all font-mono text-xs text-text-muted">{item.case_id}</span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">{item.title}</h1>
        <StatusBadge status={item.status} />
      </div>

      <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 font-mono text-xs text-text-muted">
        <span>Created {formatDateTime(item.created_at)}</span>
        {item.closed_at !== null && <span>Closed {formatDateTime(item.closed_at)}</span>}
      </div>
    </header>
  );
}

/**
 * The tab bar. `NavLink` rather than hand-tracked state, so the active tab is derived from the
 * URL — the URL stays the single source of truth for which tab is open (§9), and `aria-current`
 * comes from the router instead of being maintained by hand.
 *
 * Overview is marked `end`: without it, its path prefixes every sibling and it would render as
 * active on the Evidence tab too.
 */
function CaseTabs({ caseId }: { caseId: string }) {
  const tabClass = ({ isActive }: { isActive: boolean }) =>
    isActive
      ? `${TAB_BASE} border-accent text-text`
      : `${TAB_BASE} border-transparent text-text-muted hover:border-border hover:text-text`;

  return (
    <nav aria-label="Case sections" className="flex gap-1 border-b border-border">
      <NavLink end to={`/cases/${caseId}`} className={tabClass}>
        Overview
      </NavLink>
      <NavLink to={`/cases/${caseId}/evidence`} className={tabClass}>
        Evidence
      </NavLink>
    </nav>
  );
}

/**
 * §12's taxonomy decides the wording. A missing case and an inaccessible one deliberately read
 * the same, because the API keeps that distinction ambiguous on purpose and the UI must not
 * leak it back.
 */
function CaseDetailError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { title, detail, retryable } = describeApiError(error);
  return (
    <div role="alert" className={`${PANEL} px-4 py-10 text-center`}>
      <p className="text-sm font-medium text-text">{title}</p>
      <p className="mx-auto mt-1 max-w-prose text-sm text-text-muted">{detail}</p>
      {retryable && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 rounded border border-border px-3 py-1.5 font-mono text-xs font-medium uppercase tracking-wider hover:bg-canvas"
        >
          Try again
        </button>
      )}
    </div>
  );
}

export function CaseDetailPage() {
  const { caseId } = useParams<{ caseId: string }>();
  const { data, isPending, isError, error, refetch } = useCase(caseId);

  return (
    <section className="space-y-4">
      <BackToCases />

      {isPending ? (
        <CaseDetailSkeleton />
      ) : isError ? (
        <CaseDetailError
          error={error}
          onRetry={() => {
            void refetch();
          }}
        />
      ) : (
        <>
          <CommandHeader item={data} />
          {/*
           * The tab bar renders only once the case has loaded. Tabs over a case that failed to
           * load would invite navigation into sections that cannot resolve either.
           */}
          <CaseTabs caseId={data.case_id} />
          <Outlet />
        </>
      )}
    </section>
  );
}
