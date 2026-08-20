// @ts-check
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

/**
 * Flat ESLint config (ADR-0016 §7). Correctness lives here; formatting is Prettier's job, so no
 * stylistic rules are enabled and the two never fight.
 */
export default tseslint.config(
  { ignores: ["dist", "node_modules", "coverage"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.strictTypeChecked],
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: {
        project: ["./tsconfig.app.json"],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],

      // security-architecture.md §35: a bearer token must never reach localStorage or
      // sessionStorage. The auth store keeps it in memory (see shared/auth/token-store.ts);
      // this makes a regression a lint error rather than a review catch.
      "no-restricted-globals": [
        "error",
        {
          name: "localStorage",
          message: "Never persist session state (security-architecture §35).",
        },
        {
          name: "sessionStorage",
          message: "Never persist session state (security-architecture §35).",
        },
      ],
      "no-restricted-properties": [
        "error",
        { object: "window", property: "localStorage", message: "security-architecture §35." },
        { object: "window", property: "sessionStorage", message: "security-architecture §35." },
      ],
    },
  },
  {
    // Config files are Node-side and are not part of the app's type-checked project.
    files: ["*.{js,ts}"],
    languageOptions: { globals: globals.node },
    ...tseslint.configs.disableTypeChecked,
  },
);
