/**
 * Chain-of-custody ledger query hook (frontend-architecture.md §10, §26).
 *
 * Separate from `useEvidenceItem` rather than folded into it, because the two endpoints are
 * authorized differently and fail independently: `GET /evidence/{id}` requires the `investigator`
 * role, while `GET /evidence/{id}/custody-events` admits `investigator` *or* `compliance`. A
 * compliance user is entitled to the ledger and not the item, so a combined hook would deny them
 * a view they have every right to see.
 *
 * `useQuery`, not `useInfiniteQuery`: the endpoint returns the full ledger in one response
 * (`has_more: false`, null cursor).
 */

import { useQuery } from "@tanstack/react-query";

import type { ListEnvelope } from "@/shared/api/envelope";

import { getEvidenceCustody } from "./getEvidenceCustody";
import { evidenceQueryKeys } from "./useEvidenceItem";
import type { CustodyEvent } from "../types";

export function useEvidenceCustody(evidenceId: string | undefined) {
  const query = useQuery<ListEnvelope<CustodyEvent>>({
    queryKey: evidenceQueryKeys.custody(evidenceId ?? ""),
    queryFn: ({ signal }) => getEvidenceCustody(evidenceId ?? "", signal),
    enabled: evidenceId !== undefined && evidenceId.length > 0,
  });

  return {
    ...query,
    /**
     * Rendered in exactly the order the server returned them. `api-design.md` §5 is explicit that
     * this ledger has a fixed order — `sequence_number` ascending, "never resorted ... this is a
     * legal ledger, not a flexible list view" — so the client does not sort it, not even
     * defensively.
     */
    events: query.data?.data ?? [],
  };
}
