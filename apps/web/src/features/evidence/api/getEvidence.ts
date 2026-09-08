/**
 * `GET /api/v1/evidence` fetcher (api-design.md §5).
 *
 * Returns the whole `ListEnvelope`: unlike the case-evidence and custody endpoints, this one
 * paginates for real — the service issues a genuine keyset `next_cursor` over
 * `(ingested_at, evidence_id)` — so `pagination` is what drives the next page and must survive.
 *
 * `text` is the only filter wired up so far. The endpoint also accepts `category`,
 * `artifact_type`, and `status`; those arrive with the Evidence Explorer's filter bar (§30) rather
 * than as unused parameters here.
 */

import { apiList } from "@/shared/api/client";
import type { ListEnvelope } from "@/shared/api/envelope";
import { withPageParams, type PageRequest } from "@/shared/api/pagination";

import type { Evidence } from "../types";
import { EVIDENCE_PATH } from "./getEvidenceItem";

/**
 * Server-side filters for the evidence list.
 *
 * `text` is a case-insensitive substring match over `title` **and** `description` — not a
 * full-text or fuzzy search, and not a match on identifiers. Any UI that offers it should say so,
 * or an analyst pasting an evidence ID into the box will get an empty list and conclude the item
 * is missing.
 */
export interface EvidenceFilters {
  text?: string;
}

export function getEvidence(
  params: PageRequest & EvidenceFilters = {},
  signal?: AbortSignal,
): Promise<ListEnvelope<Evidence>> {
  const { text, ...page } = params;
  // `withPageParams` always emits `?limit=...`, so appending with `&` is safe.
  const base = withPageParams(EVIDENCE_PATH, page);
  const path =
    text !== undefined && text.length > 0 ? `${base}&text=${encodeURIComponent(text)}` : base;

  return apiList<Evidence>(path, signal ? { signal } : {});
}
