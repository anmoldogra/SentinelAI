import { useState } from "react";
import { Link } from "react-router-dom";

import { describeApiError } from "@/shared/api/errors";

import { useCases } from "../api/useCases";
import { CreateCaseModal } from "../components/CreateCaseModal";
import { StatusBadge } from "../components/StatusBadge";
import { formatDate } from "../format";
import type { Case } from "../types";

/**
 * Case Dashboard — the case list from `GET /api/v1/cases` (frontend-architecture.md §25).
 *
 * Read-only: creating and editing cases are separate increments. Filtering and sorting are too,
 * so this renders the API's default ordering (newest first, keyset-paginated) with an explicit
 * "Load more" rather than inventing client-side controls the endpoint would not honour.
 */

/** Truncated for scanning; the full value is in `title` for copy/inspect. */
function ShortId({ value }: { value: string }) {
  return (
    <span className="font-mono text-xs text-text-muted" title={value}>
      {value.slice(0, 8)}
    </span>
  );
}

/**
 * §13: a skeleton that matches the eventual layout, not a spinner — so the page does not reflow
 * when data lands.
 */
function CaseListSkeleton() {
  return (
    <div className="divide-y divide-border" aria-hidden="true">
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className="flex items-center gap-4 px-4 py-3">
          <div className="h-4 w-16 animate-pulse rounded bg-border" />
          <div className="h-4 flex-1 animate-pulse rounded bg-border" />
          <div className="h-5 w-16 animate-pulse rounded-full bg-border" />
          <div className="h-4 w-20 animate-pulse rounded bg-border" />
        </div>
      ))}
    </div>
  );
}

/** §12: the taxonomy decides the wording and whether retrying is even offered. */
function CaseListError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { title, detail, retryable } = describeApiError(error);
  return (
    <div role="alert" className="px-4 py-10 text-center">
      <p className="text-sm font-medium text-text">{title}</p>
      <p className="mx-auto mt-1 max-w-prose text-sm text-text-muted">{detail}</p>
      {retryable && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 rounded border border-border px-3 py-1.5 text-sm font-medium hover:bg-canvas"
        >
          Try again
        </button>
      )}
    </div>
  );
}

function CaseRow({ item }: { item: Case }) {
  return (
    <tr className="hover:bg-canvas">
      <td className="px-4 py-3">
        <ShortId value={item.case_id} />
      </td>
      <td className="px-4 py-3">
        {/*
         * A real anchor in the cell, not an onClick on the <tr>: only a link is keyboard
         * reachable, announced as a link, and openable in a new tab or middle-clicked — all of
         * which §36's WCAG 2.1 AA target and §31's table semantics require.
         */}
        <Link
          to={`/cases/${item.case_id}`}
          className="font-medium hover:underline focus-visible:underline focus-visible:outline-none"
        >
          {item.title}
        </Link>
        {item.description && (
          <span className="ml-2 text-sm text-text-muted">{item.description}</span>
        )}
      </td>
      <td className="px-4 py-3">
        <StatusBadge status={item.status} />
      </td>
      <td className="px-4 py-3 text-sm text-text-muted">{formatDate(item.created_at)}</td>
    </tr>
  );
}

export function CaseDashboardPage() {
  const {
    cases,
    isPending,
    isError,
    error,
    refetch,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
  } = useCases();

  // Transient UI state, owned by the component that uses it (§9) — not lifted into a store.
  const [isCreateOpen, setCreateOpen] = useState(false);

  return (
    <section aria-labelledby="cases-heading" className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <h1 id="cases-heading" className="text-2xl font-semibold tracking-tight">
          Cases
        </h1>
        <button
          type="button"
          onClick={() => {
            setCreateOpen(true);
          }}
          className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-accent-contrast hover:opacity-90"
        >
          New case
        </button>
      </div>

      <CreateCaseModal
        open={isCreateOpen}
        onClose={() => {
          setCreateOpen(false);
        }}
      />

      <div className="overflow-hidden rounded-lg border border-border bg-surface">
        {isPending ? (
          <CaseListSkeleton />
        ) : isError ? (
          <CaseListError
            error={error}
            onRetry={() => {
              void refetch();
            }}
          />
        ) : cases.length === 0 ? (
          <p className="px-4 py-10 text-center text-sm text-text-muted">No cases yet.</p>
        ) : (
          <table className="w-full text-left">
            <thead className="border-b border-border text-xs uppercase tracking-wide text-text-muted">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">
                  ID
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Title
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Status
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Created
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {cases.map((item) => (
                <CaseRow key={item.case_id} item={item} />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {hasNextPage && (
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => {
              void fetchNextPage();
            }}
            disabled={isFetchingNextPage}
            className="rounded border border-border px-4 py-2 text-sm font-medium hover:bg-surface disabled:opacity-60"
          >
            {isFetchingNextPage ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </section>
  );
}
