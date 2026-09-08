/**
 * `POST /api/v1/cases` fetcher (api-design.md §7).
 *
 * Returns the created `Case` — `apiRequest` unwraps the envelope's `data`, and the response body
 * is the full `CaseRead`, so the caller gets the server-assigned `case_id`, `status`, and
 * `created_at` rather than having to guess them.
 *
 * The `Idempotency-Key` this endpoint expects (§2.9) is not set here: the shared client adds one
 * to every mutating request automatically, and duplicating it per-feature is exactly what that
 * central layer exists to prevent.
 */

import { apiRequest } from "@/shared/api/client";

import type { Case, CreateCaseDTO } from "../types";
import { CASES_PATH } from "./getCases";

export function createCase(input: CreateCaseDTO): Promise<Case> {
  return apiRequest<Case>(CASES_PATH, { method: "POST", body: input });
}
