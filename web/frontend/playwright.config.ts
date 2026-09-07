import { defineConfig } from "@playwright/test";

// The API serves the built bundle, so the browser talks to one origin and the
// test exercises the same wiring a deployment uses.
const port = Number(process.env.ORCA_WEB_E2E_PORT ?? 8000);

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 30_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  webServer: {
    command: [
      "python3 -m uvicorn web.api.app:create_app --factory",
      `--host 127.0.0.1 --port ${port}`,
    ].join(" "),
    cwd: "../..",
    url: `http://127.0.0.1:${port}/api/v1/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
