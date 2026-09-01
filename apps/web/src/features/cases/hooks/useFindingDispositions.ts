import { useSyncExternalStore } from "react";

import {
  disposeFinding,
  getFindingsSnapshot,
  subscribeFindings,
} from "@/shared/mock/findings-store";
import { FINDINGS_BY_CASE } from "@/shared/mock/findings";
import type { FindingDisposition, FindingSeed } from "@/shared/mock/types";

export interface FindingWithState extends FindingSeed {
  disposition: FindingDisposition | undefined;
  busy: boolean;
  stateLabel: string;
  stateClass: string;
  confirm: () => void;
  reject: () => void;
  escalate: () => void;
}

/**
 * Derives this case's findings-with-disposition-state from the shared mock store
 * (shared/mock/findings-store.ts). Confirming/rejecting/escalating is never optimistic
 * (frontend-architecture.md §10, PRD FR-7.3): a busy row shows "Awaiting server…" and stays
 * disabled until the simulated server responds.
 */
export function useFindingDispositions(caseId: string): FindingWithState[] {
  const { dispositions, busyId } = useSyncExternalStore(subscribeFindings, getFindingsSnapshot);
  const seed = FINDINGS_BY_CASE[caseId] ?? [];

  return seed.map((f) => {
    const disposition = dispositions[f.id];
    const busy = busyId === f.id;
    return {
      ...f,
      disposition,
      busy,
      stateLabel: busy ? "Awaiting server…" : (disposition ?? "Unreviewed"),
      stateClass: disposition ? "tag tag-accent" : "tag tag-neutral",
      confirm: () => {
        disposeFinding(f.id, "Confirmed");
      },
      reject: () => {
        disposeFinding(f.id, "Rejected");
      },
      escalate: () => {
        disposeFinding(f.id, "Escalated");
      },
    };
  });
}
