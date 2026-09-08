/**
 * Single-case query hook (frontend-architecture.md §10).
 *
 * Takes `string | undefined` rather than `string` because the id arrives from `useParams`, which
 * cannot prove a match at compile time. Hooks cannot be called conditionally, so the query is
 * declared unconditionally and gated with `enabled` — the request is simply never issued without
 * an id.
 *
 * Retries are already handled globally: `providers.tsx` does not retry any 4xx, so a 400 from a
 * malformed id or a 404 from a missing case fails once and surfaces immediately.
 */

import { useQuery } from "@tanstack/react-query";

import { getCase } from "./getCase";
import { casesQueryKeys } from "./useCases";
import type { Case } from "../types";

export function useCase(caseId: string | undefined) {
  return useQuery<Case>({
    queryKey: casesQueryKeys.detail(caseId ?? ""),
    queryFn: ({ signal }) => getCase(caseId ?? "", signal),
    enabled: caseId !== undefined && caseId.length > 0,
  });
}
