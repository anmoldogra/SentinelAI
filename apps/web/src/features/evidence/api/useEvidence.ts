/**
 * Evidence list query hook (frontend-architecture.md §10, §33).
 *
 * `useInfiniteQuery`, because `GET /evidence` is genuinely keyset-paginated: "load the next page
 * from this cursor" is the only shape the endpoint supports, and a single-page read silently
 * hides everything past the first 50 items.
 *
 * **`placeholderData: keepPreviousData` is doing real work here.** Typing in a search box changes
 * the query key on every debounced keystroke, and without it each change would drop the hook back
 * to `isPending` — blanking the list to a skeleton and collapsing the scroll position mid-search.
 * Keeping the previous page rendered while the next result loads is what makes search feel like
 * filtering rather than repeated reloading; `isFetching` is what tells the UI to show a quiet
 * refreshing hint instead.
 *
 * `enabled` matters: the evidence list is a whole-table read no case screen needs until an analyst
 * actually opens a picker, so the caller gates it rather than paying for it on every page load.
 */

import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";

import type { ListEnvelope } from "@/shared/api/envelope";
import { flattenPages, nextCursor } from "@/shared/api/pagination";

import { getEvidence, type EvidenceFilters } from "./getEvidence";
import { evidenceQueryKeys } from "./useEvidenceItem";
import type { Evidence } from "../types";

export function useEvidence(options: { enabled?: boolean } & EvidenceFilters = {}) {
  const { enabled, ...filters } = options;

  // An empty search is *no* filter, not a filter matching everything: normalising it here keeps
  // "" and undefined on the same cache key instead of splitting them into two identical queries.
  const text = filters.text?.trim() ?? "";
  const activeFilters: EvidenceFilters = text.length > 0 ? { text } : {};

  const query = useInfiniteQuery<ListEnvelope<Evidence>>({
    queryKey: evidenceQueryKeys.list(activeFilters),
    queryFn: ({ pageParam, signal }) =>
      getEvidence({ ...activeFilters, cursor: pageParam as string | null }, signal),
    initialPageParam: null,
    getNextPageParam: nextCursor,
    enabled: enabled ?? true,
    placeholderData: keepPreviousData,
  });

  return {
    ...query,
    /** Every fetched page flattened into the single list the picker renders. */
    evidence: flattenPages(query.data?.pages),
  };
}
