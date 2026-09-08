/**
 * Evidence-unlinking mutation (`DELETE /api/v1/cases/{case_id}/evidence/{evidence_id}`).
 *
 * **Never optimistic**, for the same reason as `useLinkEvidence` and with more force: §10 reserves
 * optimistic updates for low-risk, reversible actions, and detaching evidence from a case is an
 * evidentiary act the server records — it publishes `case.evidence_unlinked` from the outbox and
 * writes an audit entry. Removing the row before the server confirms would show evidence gone from
 * a case it is still attached to, and if the request then failed the analyst would have been shown
 * a state that never existed. The ledger is refetched instead.
 *
 * `onSuccess` **returns** the invalidation promise, matching `useLinkEvidence`: React Query holds
 * the mutation pending until the list has refreshed, so the confirmation dialog closes onto an
 * already-correct table rather than one that updates a moment later.
 *
 * The endpoint answers `204 No Content`; the shared client maps that to `undefined`, so that — not
 * `void` — is the mutation's data type. It is the literal value the promise resolves with. Its
 * `Idempotency-Key` is added by the client for every mutating method (api-design.md §2.9) and is
 * deliberately not constructed here.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiRequest } from "@/shared/api/client";

import { CASES_PATH } from "./getCases";
import { casesQueryKeys } from "./useCases";

export function useUnlinkEvidence(caseId: string | undefined) {
  const queryClient = useQueryClient();

  return useMutation<undefined, Error, string>({
    mutationFn: (evidenceId) => {
      // A mutation cannot be declared conditionally, so a missing id is rejected here rather than
      // firing a DELETE at a malformed URL.
      if (caseId === undefined || caseId.length === 0) {
        return Promise.reject(new Error("No case is selected."));
      }
      return apiRequest<undefined>(
        `${CASES_PATH}/${encodeURIComponent(caseId)}/evidence/${encodeURIComponent(evidenceId)}`,
        { method: "DELETE" },
      );
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: casesQueryKeys.evidence(caseId ?? "") }),
  });
}
