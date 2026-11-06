import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import eslintReact from "@eslint-react/eslint-plugin";
import { defineConfig, globalIgnores } from "eslint/config";
import tseslint from "typescript-eslint";

export default defineConfig([
  globalIgnores([
    "dist",
    "node_modules",
    "graphify-out",
    "storage",
    ".venv",
    ".opencode",
  ]),
  {
    files: ["resources/js/**/*.{ts,tsx}"],
    extends: [
      tseslint.configs.base,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      parserOptions: {
        project: ["./tsconfig.app.json", "./tsconfig.node.json"],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      "@eslint-react": eslintReact,
    },
    rules: {
      ...eslintReact.configs["recommended-typescript"].rules,
      "@eslint-react/no-array-index-key": "off",
      "react-refresh/only-export-components": "off",
    },
  },
]);
