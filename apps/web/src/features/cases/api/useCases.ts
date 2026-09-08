/**
 * Case-list query hook (frontend-architecture.md §10).
 *
 * `useInfiniteQuery`, not `useQuery`: `GET /cases` is keyset-paginated (api-design.md §2.5) with
 * no total count, so "load the next page from this cursor" is the only shape the API supports.
 *
 * The fetched data lives solely in the React Query cache (§9) — it is never copied into another
 * store, which would create a second source of truth that can drift.
 */

import { useInfiniteQuery } from "@tanstack/react-query";

import type { ListEnvelope } from "@/shared/api/envelope";
import { flattenPages, nextCursor, type PageRequest } from "@/shared/api/pagination";

import { getCases } from "./getCases";
import type { Case } from "../types";

/** Namespaced so a detail query invalidates predictably alongside the lists. */
export const casesQueryKeys = {
  all: ["cases"] as const,
  list: (page: PageRequest = {}) => ["cases", "list", page] as const,
  detail: (caseId: string) => ["cases", "detail", caseId] as const,
  /** Shaped exactly as frontend-architecture.md's Route-to-Data Mapping specifies. */
  evidence: (caseId: string) => ["cases", caseId, "evidence"] as const,
};

export function useCases(page: PageRequest = {}) {
  const query = useInfiniteQuery<ListEnvelope<Case>>({
    queryKey: casesQueryKeys.list(page),
    queryFn: ({ pageParam, signal }) =>
      getCases({ ...page, cursor: pageParam as string | null }, signal),
    initialPageParam: null,
    getNextPageParam: nextCursor,
  });

  return {
    ...query,
    /** Every page flattened into the single list the table renders. */
    cases: flattenPages(query.data?.pages),
  };
}
