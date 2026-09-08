import { defineConfig, devices } from "@playwright/test";

// The API serves the built bundle, so the browser talks to one origin and the
// test exercises the same wiring a deployment uses.
const port = Number(process.env.ORCA_WEB_E2E_PORT ?? 8000);

// docs/web/mvp.md: Chrome, Edge, and Firefox are release-blocking; Safari is
// tested but non-blocking. Each engine/channel gets its own named Playwright
// project so a run can pick exactly the browsers it wants with `--project`.
//
// Playwright has no config-level "this project's failures don't fail the
// run" flag (checked against the installed @playwright/test types — the only
// per-test escape hatches are `test.skip`/`test.fixme`/`test.fail`, which
// mark a test as *expected* to fail rather than make a passing suite's
// failure non-fatal). The mechanism that actually reads cleanly is therefore
// at the script level, not a config flag: package.json's "e2e:matrix" runs
// the three blocking projects as one invocation (any failure fails it), and
// "e2e:webkit" runs Safari's stand-in as its own invocation; "e2e:all" runs
// both but deliberately does not propagate webkit's exit code — see
// package.json. `npm run e2e` with no project selected stays Chromium-only,
// which is today's behavior and what keeps a local run fast by default.
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
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    // Edge is Chromium underneath, but it is exercised as its own installed
    // channel rather than assumed identical to Chrome.
    {
      name: "msedge",
      use: {
        ...devices["Desktop Edge"],
        channel: "msedge",
        // Edge runs a SmartScreen reputation check on every download, which has
        // no answer for a file served from 127.0.0.1 and takes the headless
        // browser process down with it. The download itself is what the suite
        // is testing, so the check is turned off rather than the test.
        launchOptions: {
          args: [
            "--disable-features=msDownloadsHub,msEdgeDownloadBubble",
            "--safebrowsing-disable-download-protection",
          ],
        },
      },
    },
    // Safari itself cannot be automated outside macOS; WebKit is Playwright's
    // engine for it and the standard stand-in for Safari coverage elsewhere.
    // Non-blocking per docs/web/mvp.md — see the script-level note above.
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
  ],
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
