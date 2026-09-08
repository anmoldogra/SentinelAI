/**
 * `GET /api/v1/cases/{case_id}/evidence` fetcher (api-design.md §7).
 *
 * Returns the whole `ListEnvelope` for consistency with `getCases`, even though this endpoint
 * does not currently paginate: the router answers with `has_more: false` and a null cursor on
 * every call, returning the case's full link set in one response. Keeping the envelope means the
 * day it does start paginating is a change in the hook, not a change in this fetcher's contract.
 *
 * The id is percent-encoded before being spliced into the path — it comes straight off the URL,
 * and the endpoint's `case_id: UUID` rejects anything malformed with `400 VALIDATION_FAILED`.
 */

import { apiList } from "@/shared/api/client";
import type { ListEnvelope } from "@/shared/api/envelope";

import type { CaseEvidenceLink } from "../types";
import { CASES_PATH } from "./getCases";

export function getCaseEvidence(
  caseId: string,
  signal?: AbortSignal,
): Promise<ListEnvelope<CaseEvidenceLink>> {
  return apiList<CaseEvidenceLink>(
    `${CASES_PATH}/${encodeURIComponent(caseId)}/evidence`,
    signal ? { signal } : {},
  );
}
