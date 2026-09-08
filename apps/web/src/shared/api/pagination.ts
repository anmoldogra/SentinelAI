/**
 * Cursor-pagination helpers (`api-design.md` §2.5, frontend-architecture.md §33).
 *
 * The backend uses **keyset** pagination on every append-heavy collection: an opaque,
 * server-issued `cursor` plus a `limit`, with `pagination.next_cursor` / `pagination.has_more`
 * on the response. There is no page number and no total count, by design — so this layer never
 * offers an `offset`/`page` shape that the API could not honour.
 *
 * Written once here so each feature's query hooks plug straight into React Query's
 * infinite-query pattern instead of re-deriving cursor handling.
 */

import type { ListEnvelope } from "./envelope";

/** `api-design.md` §2.5: default 50, max 200. */
export const DEFAULT_PAGE_LIMIT = 50;
export const MAX_PAGE_LIMIT = 200;

/** One page request. `cursor` is `null`/absent for the first page. */
export interface PageRequest {
  cursor?: string | null;
  limit?: number;
}

/** Append `limit`/`cursor` to a path, omitting the cursor on the first page. */
export function withPageParams(path: string, page: PageRequest = {}): string {
  const params = new URLSearchParams();
  params.set("limit", String(Math.min(page.limit ?? DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT)));
  if (page.cursor) {
    params.set("cursor", page.cursor);
  }
  return `${path}?${params.toString()}`;
}

/**
 * `getNextPageParam` for React Query's `useInfiniteQuery`.
 *
 * Returns `undefined` — not `null` — when there is no further page, because that is what React
 * Query treats as "no more pages" (`hasNextPage === false`). Guards on `has_more` *and* a
 * non-null cursor: a truthful backend never sends one without the other, but paging forever on a
 * missing cursor is a nastier failure than stopping one page early.
 */
export function nextCursor<T>(lastPage: ListEnvelope<T>): string | undefined {
  if (!lastPage.pagination.has_more) {
    return undefined;
  }
  return lastPage.pagination.next_cursor ?? undefined;
}

/** Flatten infinite-query pages into the single list a table renders. */
export function flattenPages<T>(pages: readonly ListEnvelope<T>[] | undefined): T[] {
  return (pages ?? []).flatMap((page) => page.data);
}
