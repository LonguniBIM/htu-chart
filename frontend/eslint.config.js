import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/ban-ts-comment": [
        "error",
        {
          "ts-ignore": true,
          "ts-expect-error": true,
          "ts-nocheck": true,
        },
      ],
      "no-restricted-globals": [
        "error",
        {
          name: "localStorage",
          message: "SEC-002: Never store auth tokens, CSRF tokens, or credentials in localStorage.",
        },
        {
          name: "sessionStorage",
          message: "SEC-002: Never store auth tokens or CSRF tokens in sessionStorage.",
        },
      ],
    },
  },
  {
    files: ["src/drawings/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: ["**/replay/**", "**/backtest/**", "**/agent/**"],
        },
      ],
    },
  },
  {
    files: ["src/chart/**/*.{ts,tsx}", "src/drawings/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name='setTimeout']",
          message: "SLOP-002: Raw setTimeout is forbidden in src/chart and src/drawings.",
        },
        {
          selector: "CallExpression[callee.name='setInterval']",
          message: "SLOP-002: Raw setInterval is forbidden in src/chart and src/drawings.",
        },
        {
          selector: "CallExpression[callee.name='fetch'][arguments.length<2]",
          message: "F06: Direct fetch() inside src/chart must pass an AbortSignal options object.",
        },
      ],
    },
  },
  {
    files: ["src/store/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name='fetch'][arguments.length<2]",
          message: "F06: Direct fetch() inside src/store must pass an AbortSignal options object.",
        },
      ],
    },
  },
);
