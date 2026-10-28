import { defineConfig } from "@playwright/test";

// The API serves the built bundle, so the browser talks to one origin and the
// test exercises the same wiring a deployment uses.
const port = Number(process.env.ORCA_WEB_E2E_PORT ?? 8000);

// Chromium always runs. Firefox and WebKit (Safari's engine) run when listed in
// ORCA_WEB_E2E_BROWSERS and installed, and Edge when its "msedge" channel is;
// WebKit failures are reported but do not block, per docs/web/mvp.md.
const BROWSERS = new Set(["chromium", ...(process.env.ORCA_WEB_E2E_BROWSERS ?? "").split(",").filter(Boolean)]);
const projects = [
  { name: "chromium", use: { browserName: "chromium" as const } },
  { name: "msedge", use: { browserName: "chromium" as const, channel: "msedge" } },
  { name: "firefox", use: { browserName: "firefox" as const } },
  { name: "webkit", use: { browserName: "webkit" as const } },
].filter((project) => BROWSERS.has(project.name));

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 30_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  projects,
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
