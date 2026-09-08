// @ts-check
import fs from "node:fs";
import path from "node:path";

import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

/**
 * Flat ESLint config (ADR-0016 §7). Correctness lives here; formatting is Prettier's job, so no
 * stylistic rules are enabled and the two never fight.
 */

const FEATURES_DIR = path.join(import.meta.dirname, "src", "features");

/**
 * Every feature directory, read from disk rather than hardcoded.
 *
 * Enumerating them means a newly added feature is protected the moment it exists. A hardcoded
 * list would fail in exactly the way this rule exists to prevent: the feature someone forgot to
 * register is the one that quietly accumulates boundary violations.
 */
const featureNames = fs.existsSync(FEATURES_DIR)
  ? fs
      .readdirSync(FEATURES_DIR, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort()
  : [];

/**
 * The feature-boundary rule (frontend-architecture.md §3).
 *
 * A feature's internals are private; the only sanctioned import surface is its `public.ts`. This
 * turns that from a documented convention into a build failure, which is the difference between
 * a rule that holds on day 100 and one that erodes.
 *
 * **Why one config block per feature, keyed on the *importing* feature.** Flat config *replaces*
 * a rule's options when a later block sets the same rule — it does not merge them. Writing one
 * block per *restricted* feature (each with `ignores` for its owner) would therefore leave only
 * the last block's patterns in effect and silently disable every earlier one. Scoping each block
 * to the files doing the importing means every file matches exactly one block, whose pattern list
 * names all the other features.
 *
 * **What this deliberately does not cover.** `no-restricted-imports` matches the literal import
 * specifier, not the resolved path, so a relative escape (`../../evidence/api/useEvidenceItem`)
 * is invisible to it. The codebase's convention is the `@/` alias for anything cross-feature, so
 * that is what is enforced here; closing the relative-path hole needs a resolver-aware plugin
 * (`eslint-plugin-import` or `eslint-plugin-boundaries`), which is a dependency decision rather
 * than a config one.
 *
 * `src/app/**` is intentionally out of scope: it is the composition root, and §3 puts route
 * declaration there precisely so features never import each other to navigate. It importing a
 * feature's route component is the design, not a violation.
 */
const featureBoundaryConfigs = featureNames
  .map((owner) => ({
    owner,
    patterns: featureNames
      .filter((other) => other !== owner)
      .map((other) => ({
        // Gitignore-style: everything under the feature, minus its public surface.
        group: [`@/features/${other}/**`, `!@/features/${other}/public`],
        message: `Import from "@/features/${other}/public" instead — a feature's internals are private (frontend-architecture.md §3). If what you need isn't exported there, add it deliberately.`,
      })),
  }))
  // A single-feature repo has nothing to restrict, and `patterns: []` is not a valid option.
  .filter((entry) => entry.patterns.length > 0)
  .map((entry) => ({
    files: [`src/features/${entry.owner}/**/*.{ts,tsx}`],
    rules: {
      "no-restricted-imports": /** @type {const} */ (["error", { patterns: entry.patterns }]),
    },
  }));

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
  // Declared after the base block so these `no-restricted-imports` settings are the ones in
  // effect for feature files. Nothing above sets that rule today; if something ever does, it
  // would be overridden here rather than merged — see the note on the constant above.
  ...featureBoundaryConfigs,
  {
    // Config files are Node-side and are not part of the app's type-checked project.
    files: ["*.{js,ts}"],
    languageOptions: { globals: globals.node },
    ...tseslint.configs.disableTypeChecked,
  },
);
