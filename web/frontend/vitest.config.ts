import { defineConfig } from "vitest/config";

// Unit tests cover the pure plater and preview logic; Playwright owns e2e/.
export default defineConfig({
  test: { include: ["src/**/*.test.ts"] },
});
