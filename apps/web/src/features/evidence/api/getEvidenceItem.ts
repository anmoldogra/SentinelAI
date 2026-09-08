/**
 * `GET /api/v1/evidence/{evidence_id}` fetcher (api-design.md §5).
 *
 * The id is percent-encoded before being spliced into the path: it arrives from a link record or
 * a URL, and the endpoint's `evidence_id: UUID` rejects anything malformed with a
 * `400 VALIDATION_FAILED` rather than a 404.
 */

import { apiRequest } from "@/shared/api/client";

import type { Evidence } from "../types";

export const EVIDENCE_PATH = "/evidence";

export function getEvidenceItem(evidenceId: string, signal?: AbortSignal): Promise<Evidence> {
  return apiRequest<Evidence>(
    `${EVIDENCE_PATH}/${encodeURIComponent(evidenceId)}`,
    signal ? { signal } : {},
  );
}
