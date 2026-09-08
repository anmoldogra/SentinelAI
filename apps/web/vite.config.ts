import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
// `vitest/config` rather than `vite` — it is Vite's own `defineConfig` widened with the `test`
// key, so the test block below is type-checked instead of needing a triple-slash reference.
import { defineConfig } from "vitest/config";

/**
 * Vite config for apps/web (ADR-0016).
 *
 * The dev server proxies `/api` to the backend so the browser sees a single origin: no CORS
 * configuration in development, and — more importantly — the app calls the same relative
 * `/api/v1/...` paths in dev and in production, where the static bundle is served alongside the
 * API. There is no build-time API base URL to get wrong per environment.
 *
 * Vitest shares this config rather than owning a second one, so the test run resolves modules and
 * the `@` alias exactly as the app does — a separate config is a second source of truth that
 * drifts, and tests passing against different resolution than production is worse than no tests.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env["VITE_API_PROXY_TARGET"] ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    /*
     * `node`, not `jsdom`: the only suite today exercises a pure crypto utility with no DOM, and
     * Node's own Web Crypto is the same SubtleCrypto implementation the browser exposes. A DOM
     * environment would add startup cost and, worse, a jsdom polyfill could diverge from the real
     * platform API this code depends on. UI tests, when they arrive, can opt into `jsdom` per file.
     */
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
  build: {
    // Sourcemaps are load-bearing for triaging a production incident in an air-gapped
    // deployment, where attaching a debugger is not an option.
    sourcemap: true,
    rollupOptions: {
      output: {
        /*
         * Keep the vendor core in its own chunk: frontend-architecture.md §2 flags the SPA
         * payload as a real cost on low-bandwidth on-prem networks, and §39-41 treat splitting
         * as a requirement rather than polish.
         *
         * **These entries are module ids, not package names.** Rollup matches what the app
         * actually imports, so listing `"react-dom"` alone does nothing for an app that imports
         * `react-dom/client` — a distinct module id, whose (large) graph then falls back into the
         * entry chunk. Naming every entry point the app really uses is what keeps the framework
         * out of `index`: `react-dom/client` for the root render and `react/jsx-runtime` for the
         * automatic JSX transform, which every `.tsx` file pulls in whether or not it names React.
         *
         * The payoff is cache lifetime, not just first load. A vendor chunk changes only when a
         * dependency is upgraded, so it survives every application deploy in the browser cache —
         * whereas anything sharing the entry chunk is re-downloaded on each release.
         */
        manualChunks: {
          "vendor-react": [
            "react",
            "react/jsx-runtime",
            "react-dom",
            "react-dom/client",
            "react-router-dom",
          ],
          "vendor-query": ["@tanstack/react-query"],
        },
      },
    },
  },
});
