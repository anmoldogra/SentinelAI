/**
 * Evidence-linking mutation (`POST /api/v1/cases/{case_id}/evidence`).
 *
 * **Never optimistic.** §10 reserves optimistic updates for low-risk, reversible actions, and
 * linking evidence to a case is neither: it is an evidentiary act that appends to the case's
 * record and publishes `case.evidence_linked` from the server's outbox. Showing the link before
 * the server has committed it would put a claim on screen that the audit trail does not yet
 * support — and if the write then failed, the analyst would have seen evidence attached to a case
 * it was never attached to. The list is refetched instead.
 *
 * `onSuccess` **returns** the invalidation promise deliberately, matching `useCreateCase`: React
 * Query holds the mutation pending until it settles, so by the time the caller's own `onSuccess`
 * runs the ledger has already refreshed. Closing the modal then reveals the new row rather than a
 * stale table that updates a moment later.
 *
 * The `Idempotency-Key` this POST needs (api-design.md §2.9) is added by the shared client for
 * every mutating method — it is deliberately not constructed here.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiRequest } from "@/shared/api/client";

import { CASES_PATH } from "./getCases";
import { casesQueryKeys } from "./useCases";
import type { CaseEvidenceLink } from "../types";

/** The request body, mirroring the backend's `EvidenceLinkCreate`. Exactly one field. */
export interface LinkEvidenceDTO {
  evidence_id: string;
}

/**
 * Kept in this file rather than split into a `linkEvidence.ts` fetcher: it has one caller, and
 * the codebase's fetcher/hook split exists to share a fetcher between hooks. It moves out the
 * moment a second consumer appears.
 */
function linkEvidence(caseId: string, payload: LinkEvidenceDTO): Promise<CaseEvidenceLink> {
  return apiRequest<CaseEvidenceLink>(`${CASES_PATH}/${encodeURIComponent(caseId)}/evidence`, {
    method: "POST",
    body: payload,
  });
}

export function useLinkEvidence(caseId: string | undefined) {
  const queryClient = useQueryClient();

  return useMutation<CaseEvidenceLink, Error, LinkEvidenceDTO>({
    mutationFn: (payload) => {
      // A mutation cannot be declared conditionally, so the missing-id case is rejected here
      // rather than firing a request at a malformed URL.
      if (caseId === undefined || caseId.length === 0) {
        return Promise.reject(new Error("No case is selected."));
      }
      return linkEvidence(caseId, payload);
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: casesQueryKeys.evidence(caseId ?? "") }),
  });
}
