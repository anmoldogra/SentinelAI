/**
 * `GET /api/v1/cases` fetcher (api-design.md §7).
 *
 * Returns the whole `ListEnvelope` rather than just `data`, because the cursor in `pagination`
 * is what drives the next page — unwrapping here would throw it away.
 */

import { apiList } from "@/shared/api/client";
import type { ListEnvelope } from "@/shared/api/envelope";
import { withPageParams, type PageRequest } from "@/shared/api/pagination";

import type { Case } from "../types";

export const CASES_PATH = "/cases";

export function getCases(
  page: PageRequest = {},
  signal?: AbortSignal,
): Promise<ListEnvelope<Case>> {
  return apiList<Case>(withPageParams(CASES_PATH, page), signal ? { signal } : {});
}
