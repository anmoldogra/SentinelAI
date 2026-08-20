import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * Vite config for apps/web (ADR-0016).
 *
 * The dev server proxies `/api` to the backend so the browser sees a single origin: no CORS
 * configuration in development, and — more importantly — the app calls the same relative
 * `/api/v1/...` paths in dev and in production, where the static bundle is served alongside the
 * API. There is no build-time API base URL to get wrong per environment.
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
  build: {
    // Sourcemaps are load-bearing for triaging a production incident in an air-gapped
    // deployment, where attaching a debugger is not an option.
    sourcemap: true,
    rollupOptions: {
      output: {
        // Keep the vendor core in its own chunk: frontend-architecture.md §2 flags the SPA
        // payload as a real cost on low-bandwidth on-prem networks, and §39-41 treat splitting
        // as a requirement rather than polish. Feature-level splitting arrives with the features.
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-query": ["@tanstack/react-query"],
        },
      },
    },
  },
});
