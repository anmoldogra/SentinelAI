/**
 * `GET /api/v1/cases/{case_id}` fetcher (api-design.md §7).
 *
 * The id comes straight off the URL, so it is percent-encoded before being spliced into the path
 * — the value is user-controlled, and the endpoint's `case_id: UUID` will reject anything
 * malformed with a `400 VALIDATION_FAILED` rather than a 404.
 */

import { apiRequest } from "@/shared/api/client";

import type { Case } from "../types";
import { CASES_PATH } from "./getCases";

export function getCase(caseId: string, signal?: AbortSignal): Promise<Case> {
  return apiRequest<Case>(`${CASES_PATH}/${encodeURIComponent(caseId)}`, signal ? { signal } : {});
}
