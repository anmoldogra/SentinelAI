/// <reference types="vite/client" />

/** Environment variables this app reads. Typed so a mistyped name is a compile error. */
interface ImportMetaEnv {
  /**
   * Dev-only bearer token (see shared/auth/token-store.ts). Never set in production: the block
   * that reads it is compiled out of a production build.
   */
  readonly VITE_DEV_ACCESS_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
