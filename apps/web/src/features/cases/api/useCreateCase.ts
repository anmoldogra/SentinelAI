/**
 * Case-creation mutation hook (frontend-architecture.md §10).
 *
 * Not optimistic. A case is a server-owned record — it gets its `case_id`, `status`, and
 * `created_at` from the backend — so showing a provisional row before the server confirms would
 * mean rendering values the UI invented. The list is refetched instead.
 *
 * `onSuccess` **returns** the invalidation promise deliberately: React Query keeps the mutation
 * pending until it settles, so by the time the caller's own `onSuccess` runs the dashboard has
 * already refreshed. Closing the modal then reveals the new case rather than a stale list that
 * updates a moment later.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { createCase } from "./createCase";
import { casesQueryKeys } from "./useCases";
import type { Case, CreateCaseDTO } from "../types";

export function useCreateCase() {
  const queryClient = useQueryClient();

  return useMutation<Case, Error, CreateCaseDTO>({
    mutationFn: createCase,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: casesQueryKeys.all }),
  });
}
