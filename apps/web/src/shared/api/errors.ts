/**
 * The client-side error taxonomy (frontend-architecture.md §12).
 *
 * §12 is explicit that errors are "never shown as an undifferentiated 'something went wrong'" —
 * each documented `api-design.md` §2.4 code maps to a specific treatment. This translates a
 * failure into what the UI should say and whether retrying is worth offering, so every feature
 * gets the same taxonomy instead of inventing its own copy.
 */

import { ApiError } from "./envelope";

export interface ErrorPresentation {
  title: string;
  detail: string;
  /** Whether a retry affordance makes sense — a 403 will not become a 200 on retry. */
  retryable: boolean;
}

export function describeApiError(error: unknown): ErrorPresentation {
  if (!(error instanceof ApiError)) {
    return {
      title: "Something went wrong",
      detail:
        error instanceof Error && error.message
          ? error.message
          : "The request could not be completed.",
      retryable: true,
    };
  }

  switch (error.code) {
    case "UNAUTHENTICATED":
      // §12 says redirect to /login. That route does not exist yet, so until the auth slice
      // lands the honest thing is to say so rather than bounce to a 404.
      return {
        title: "Not signed in",
        detail:
          "This session is not authenticated. Sign-in is not built yet — see the dev-token seam " +
          "in shared/auth/token-store.ts for local development.",
        retryable: false,
      };

    case "FORBIDDEN":
    case "NOT_FOUND":
      // Deliberately one shared message: §12 requires preserving the API's existence ambiguity,
      // so the UI must never reveal whether a resource exists but is inaccessible.
      return {
        title: "Unavailable",
        detail: "This resource is not available with your current access.",
        retryable: false,
      };

    case "RATE_LIMITED":
      return {
        title: "Too many requests",
        detail: "Slow down and try again shortly.",
        retryable: true,
      };

    case "CONFLICT":
    case "IDEMPOTENCY_KEY_CONFLICT":
      return {
        title: "Conflicting change",
        detail: "This changed elsewhere. Reload and try again.",
        retryable: true,
      };

    case "SERVICE_UNAVAILABLE":
    case "INTERNAL_ERROR":
      return {
        title: "Server error",
        detail: "The server could not complete the request. This is usually temporary.",
        retryable: true,
      };

    default:
      return {
        title: "Request failed",
        detail: error.message,
        retryable: true,
      };
  }
}
