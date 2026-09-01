import type { FindingDisposition } from "./types";

/**
 * Module-level mock stand-in for the React Query cache entry a real `['relationships', ...]`
 * query would own (frontend-architecture.md §9-10) — kept outside any one component so the
 * Findings tab (features/cases) and the Reports screen, which read the same disposition state but
 * sit in different route subtrees, both observe the same values without prop-drilling or
 * requiring a shared layout ancestor.
 */

interface FindingsState {
  dispositions: Record<string, FindingDisposition>;
  busyId: string | null;
}

let state: FindingsState = { dispositions: {}, busyId: null };
const listeners = new Set<() => void>();

const SERVER_LATENCY_MS = 800;

function setState(next: FindingsState) {
  state = next;
  listeners.forEach((listener) => {
    listener();
  });
}

export function subscribeFindings(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getFindingsSnapshot(): FindingsState {
  return state;
}

/** Non-optimistic: `busyId` is set immediately, the disposition only lands after the simulated
 * server round trip (PRD FR-7.3 — never show a finding's new disposition before the server has
 * actually recorded it). */
export function disposeFinding(findingId: string, disposition: FindingDisposition): void {
  setState({ ...state, busyId: findingId });
  setTimeout(() => {
    setState({
      dispositions: { ...state.dispositions, [findingId]: disposition },
      busyId: null,
    });
  }, SERVER_LATENCY_MS);
}
