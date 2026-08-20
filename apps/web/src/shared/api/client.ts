/**
 * The single API client layer (frontend-architecture.md §11).
 *
 * Every HTTP call goes through here so `api-design.md` §2's conventions are implemented **once**
 * rather than per-feature — which is the only thing that makes them reliably followed. Feature
 * code never constructs an `Authorization`, `X-Correlation-Id`, or `Idempotency-Key` header
 * itself.
 *
 * Implemented here now: base path, envelope parsing, bearer injection, correlation ID,
 * automatic idempotency keys, and 401 notification. Deliberately NOT here yet — they belong with
 * the features that need them, and stubbing them now would be guesswork:
 *   - `If-Match` / `If-None-Match` (§11 table) — needs the ETag-carrying resource hooks.
 *   - 429 `Retry-After` handling (§12) — needs the notification surface to report it.
 *   - the infinite-query cursor helper (§33) — needs a real paginated feature.
 */

import { getAccessToken } from "@/shared/auth/token-store";

import { ApiError, type Envelope, type ErrorEnvelope, type ListEnvelope } from "./envelope";

/**
 * Relative on purpose: the dev server proxies `/api` to the backend and production serves the
 * bundle alongside the API, so there is no per-environment base URL to misconfigure.
 */
const API_BASE = "/api/v1";

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export interface RequestOptions {
  method?: string;
  body?: unknown;
  /**
   * Correlation ID for the whole user-initiated workflow (`api-design.md` §2.8). Pass the same
   * value across every call one analyst action triggers; omit it and a per-call ID is minted.
   */
  correlationId?: string;
  /** Overrides the auto-generated `Idempotency-Key` for retryable mutations (§2.9). */
  idempotencyKey?: string;
  signal?: AbortSignal;
}

/** Notified whenever the API answers 401, so the auth context can log out globally (§6). */
type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler;
}

/**
 * A correlation/idempotency identifier.
 *
 * `crypto.randomUUID` exists only in a **secure context**. TypeScript's DOM lib types it as
 * unconditionally present, but an air-gapped deployment served over plain HTTP on a LAN address
 * is not a secure context and genuinely lacks it (`frontend-architecture.md` §2's deployment
 * profiles) — so the runtime probe is real, and `globalThis` is narrowed to an optional shape to
 * say so honestly rather than suppressing the lint rule that would otherwise call it dead code.
 *
 * The fallback is for correlation and idempotency keys — identifiers that must be unique, not
 * unguessable. It is never used for anything security-sensitive.
 */
export function newCorrelationId(): string {
  const webCrypto = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  const uuid = webCrypto?.randomUUID?.();
  if (uuid !== undefined) {
    return uuid;
  }
  return `cid-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function buildHeaders(method: string, options: RequestOptions): Headers {
  const headers = new Headers({ Accept: "application/json" });

  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const token = getAccessToken();
  if (token !== null) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  headers.set("X-Correlation-Id", options.correlationId ?? newCorrelationId());

  // Every mutating call carries one, so a retried request can never double-apply (§2.9).
  if (MUTATING_METHODS.has(method)) {
    headers.set("Idempotency-Key", options.idempotencyKey ?? newCorrelationId());
  }

  return headers;
}

async function parseError(response: Response): Promise<never> {
  let body: ErrorEnvelope["error"] = {
    code: "INTERNAL_ERROR",
    message: response.statusText || "Request failed",
  };
  try {
    const parsed = (await response.json()) as Partial<ErrorEnvelope>;
    if (parsed.error) {
      body = parsed.error;
    }
  } catch {
    // A non-JSON error body (a proxy or gateway page) still has to surface as an ApiError
    // rather than a parse crash, so the default above stands.
  }
  throw new ApiError(response.status, body, response.headers.get("X-Request-Id") ?? undefined);
}

async function request(path: string, options: RequestOptions = {}): Promise<Response> {
  const method = (options.method ?? "GET").toUpperCase();

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: buildHeaders(method, options),
    body: options.body === undefined ? null : JSON.stringify(options.body),
    // The refresh-token cookie is HttpOnly; it must ride along for the session to be renewable.
    credentials: "same-origin",
    ...(options.signal ? { signal: options.signal } : {}),
  });

  // Centralized so no feature reimplements it (§11). Notify first, then still throw, so the
  // caller's error path runs normally.
  if (response.status === 401) {
    onUnauthorized?.();
  }

  if (!response.ok) {
    await parseError(response);
  }

  return response;
}

/** GET/POST/... a single resource, returning the unwrapped `data`. */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await request(path, options);
  if (response.status === 204) {
    return undefined as T;
  }
  const envelope = (await response.json()) as Envelope<T>;
  return envelope.data;
}

/**
 * Fetch a collection, keeping `pagination` intact — the cursor is what the infinite-query
 * pattern (§33) needs, so unwrapping to `data` alone would throw it away.
 */
export async function apiList<T>(
  path: string,
  options: RequestOptions = {},
): Promise<ListEnvelope<T>> {
  const response = await request(path, options);
  return (await response.json()) as ListEnvelope<T>;
}
