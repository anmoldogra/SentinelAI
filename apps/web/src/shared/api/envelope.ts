/**
 * The API response envelope from `api-design.md` §2.4, typed once so every feature's
 * data-fetching hooks (frontend-architecture.md §10) consume one consistent shape.
 */

/** Per-response metadata the server attaches to every call. */
export interface Meta {
  request_id: string;
  correlation_id: string;
}

/** Cursor pagination (`api-design.md` §2.5) — no page numbers, no total count, by design. */
export interface Pagination {
  next_cursor: string | null;
  has_more: boolean;
  limit: number;
}

/** A single-resource success envelope. */
export interface Envelope<T> {
  data: T;
  meta: Meta;
}

/** A collection success envelope. */
export interface ListEnvelope<T> {
  data: T[];
  pagination: Pagination;
  meta: Meta;
}

/** The error envelope. `code` is one of `api-design.md` §2.4's documented codes. */
export interface ErrorBody {
  code: string;
  message: string;
  details?: unknown;
}

export interface ErrorEnvelope {
  error: ErrorBody;
  meta?: Meta;
}

/**
 * A failed API call, carrying the server's documented error code alongside the HTTP status.
 *
 * Features branch on `code` rather than on `status`, because the code is the stable contract
 * (`VALIDATION_FAILED`, `LEGAL_HOLD_VIOLATION`, `CONFLICT`, ...) while several codes can share
 * one status.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  readonly requestId: string | undefined;

  constructor(status: number, body: ErrorBody, requestId?: string) {
    super(body.message || `API request failed with status ${String(status)}`);
    this.name = "ApiError";
    this.status = status;
    this.code = body.code;
    this.details = body.details;
    this.requestId = requestId;
  }
}
