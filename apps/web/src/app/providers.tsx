import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

/**
 * Application-wide providers (frontend-architecture.md §9).
 *
 * React Query is the ONLY home for server state — it is never copied into a second store, which
 * would create two sources of truth that can drift (§9). The auth context (§6) and theme
 * provider (§18) mount here too once they exist.
 */

function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Evidence and custody records are effectively immutable once written, so refetching on
        // every window focus is wasted bandwidth — a real cost on the low-bandwidth on-prem
        // networks §2 calls out. Features that need livelier data lower this per-query.
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        // A 403/404 will not become a 200 on retry; only transient failures are worth repeating.
        retry: (failureCount: number, error: unknown) => {
          const status = (error as { status?: number }).status;
          if (status !== undefined && status >= 400 && status < 500) {
            return false;
          }
          return failureCount < 2;
        },
      },
      mutations: {
        // Never retried by default: a mutation carries an Idempotency-Key, but a blind retry of
        // a human-in-the-loop action (PRD FR-7.3) is exactly what must not happen automatically.
        retry: false,
      },
    },
  });
}

export function AppProviders({ children }: { children: ReactNode }) {
  // Created once per app instance, not per render, and not at module scope (which would leak
  // one client across tests).
  const [queryClient] = useState(createQueryClient);

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
