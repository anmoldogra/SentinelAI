/**
 * `GET /api/v1/evidence/{evidence_id}/custody-events` fetcher (api-design.md §5).
 *
 * Returns the whole `ListEnvelope` for consistency with the other list fetchers, even though the
 * endpoint does not currently paginate: the router answers with `has_more: false` and a null
 * cursor, returning the full ledger in one response. `api-design.md` documents cursor pagination
 * ordered by `sequence_number` ascending, so keeping the envelope is what lets that arrive as a
 * hook change rather than a contract change.
 */

import { apiList } from "@/shared/api/client";
import type { ListEnvelope } from "@/shared/api/envelope";

import type { CustodyEvent } from "../types";
import { EVIDENCE_PATH } from "./getEvidenceItem";

export function getEvidenceCustody(
  evidenceId: string,
  signal?: AbortSignal,
): Promise<ListEnvelope<CustodyEvent>> {
  return apiList<CustodyEvent>(
    `${EVIDENCE_PATH}/${encodeURIComponent(evidenceId)}/custody-events`,
    signal ? { signal } : {},
  );
}
