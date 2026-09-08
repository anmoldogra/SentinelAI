/**
 * Case-evidence link query hook (frontend-architecture.md §10).
 *
 * `useQuery`, not `useInfiniteQuery`: the backend returns the case's entire link set in one
 * response (`has_more: false`, null cursor, always), so an infinite query would add paging
 * machinery around a collection that never has a second page. If the endpoint gains real keyset
 * pagination, this becomes an infinite query — which is exactly why `getCaseEvidence` keeps the
 * envelope rather than unwrapping it.
 *
 * Takes `string | undefined` because the id arrives from `useParams`, which cannot prove a match
 * at compile time; the query is declared unconditionally and gated with `enabled`.
 */

import { useQuery } from "@tanstack/react-query";

import type { ListEnvelope } from "@/shared/api/envelope";

import { getCaseEvidence } from "./getCaseEvidence";
import { casesQueryKeys } from "./useCases";
import type { CaseEvidenceLink } from "../types";

export function useCaseEvidence(caseId: string | undefined) {
  const query = useQuery<ListEnvelope<CaseEvidenceLink>>({
    queryKey: casesQueryKeys.evidence(caseId ?? ""),
    queryFn: ({ signal }) => getCaseEvidence(caseId ?? "", signal),
    enabled: caseId !== undefined && caseId.length > 0,
  });

  return {
    ...query,
    /** The link rows the table renders, flattened out of the envelope. */
    links: query.data?.data ?? [],
  };
}
