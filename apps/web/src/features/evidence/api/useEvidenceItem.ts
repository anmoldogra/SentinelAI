/**
 * Single-evidence query hook (frontend-architecture.md §10).
 *
 * **This is the resolution primitive for evidence referenced by other features.** A case's
 * evidence links, an entity-graph node, and a timeline entry all carry an `evidence_id` and
 * nothing else, because `ingestion.evidence` is another module's table and `database-design.md`
 * forbids the cross-schema join that would let those endpoints embed it. Resolving one id at a
 * time through the query cache is what turns those bare references into displayable metadata.
 *
 * **Why per-item and not a batch endpoint.** Rows resolve independently and the cache is keyed by
 * evidence id, so the same item referenced by two cases, a graph node, and a timeline entry is
 * fetched once and shared by all of them — and a row already in cache renders with no request at
 * all. The cost is one request per distinct uncached id, which is acceptable at a case's link
 * count and is the reason `staleTime` below is generous. A batch endpoint becomes worth building
 * when a single view routinely resolves more ids than a browser will run concurrently; it is not
 * needed to make this correct.
 *
 * Takes `string | undefined` because callers may not have an id yet; the query is declared
 * unconditionally and gated with `enabled`, since hooks cannot be called conditionally.
 */

import { useQuery } from "@tanstack/react-query";

import type { EvidenceFilters } from "./getEvidence";
import { getEvidenceItem } from "./getEvidenceItem";
import type { Evidence } from "../types";

/** Shaped exactly as frontend-architecture.md's Route-to-Data Mapping specifies. */
export const evidenceQueryKeys = {
  all: ["evidence"] as const,
  /**
   * The `"list"` segment cannot collide with `detail`: an `evidence_id` is always a UUID.
   *
   * Keyed by **filters only**, never by cursor — an infinite query keeps its pages under one key
   * and passes the cursor as `pageParam`. Putting the cursor in the key would give every page its
   * own cache entry and defeat the pagination it is meant to support.
   */
  list: (filters: Readonly<EvidenceFilters> = {}) => ["evidence", "list", filters] as const,
  detail: (evidenceId: string) => ["evidence", evidenceId] as const,
  custody: (evidenceId: string) => ["evidence", evidenceId, "custody-events"] as const,
};

export function useEvidenceItem(evidenceId: string | undefined) {
  return useQuery<Evidence>({
    queryKey: evidenceQueryKeys.detail(evidenceId ?? ""),
    queryFn: ({ signal }) => getEvidenceItem(evidenceId ?? "", signal),
    enabled: evidenceId !== undefined && evidenceId.length > 0,
    /**
     * Longer than the 30s global default. An evidence record is immutable in every field this
     * resolves except `integrity_verification_status` and `legal_hold`, and both change only
     * through an explicit action whose own mutation invalidates this key — so refetching a
     * resolved row on a timer would be pure traffic on the low-bandwidth networks §2 calls out.
     */
    staleTime: 5 * 60_000,
  });
}
