import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const CUBE = resolve(REPO_ROOT, "tests/data/20mm_cube.obj");

test.beforeAll(() => {
  if (!existsSync(CUBE)) throw new Error(`The fixture ${CUBE} is missing.`);
});

/** Wait for the profile lists the screen needs before it can submit anything. */
async function waitForProfiles(page: Page) {
  await expect(page.getByTestId("printer-select")).toBeEnabled();
  await expect(page.getByTestId("process-select")).toBeEnabled();
  await expect(page.getByTestId("filament-select")).toBeEnabled();
}

test("a browser upload produces downloadable G-code", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  await page.getByTestId("file-input").setInputFiles(CUBE);
  await expect(page.getByTestId("selected-file")).toContainText("20mm_cube.obj");

  await page.getByTestId("setting-layer_height").fill("0.28");
  await page.getByTestId("slice").click();

  await expect(page.getByTestId("job-state")).toHaveText("succeeded");
  await expect(page.getByTestId("job-stage")).toContainText("100%");
  await expect(page.getByTestId("failure")).toHaveCount(0);

  const download = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("download-gcode").click(),
  ]).then(([event]) => event);
  expect(download.suggestedFilename()).toMatch(/^job-[0-9a-f]{32}\.gcode$/);

  // The download must be the G-code the worker actually published: a 20 mm cube
  // at 0.28 mm is about 71 layers.
  const jobId = await page.getByTestId("job-id").innerText();
  const gcode = await page.request.get(`/api/v1/jobs/${jobId}/artifacts/gcode`);
  expect(gcode.ok()).toBeTruthy();
  const text = await gcode.text();
  expect(text).toContain("G1 ");
  expect(text.split(";LAYER_CHANGE").length - 1).toBeGreaterThan(40);

  const report = await page.request.get(`/api/v1/jobs/${jobId}/artifacts/result`);
  expect((await report.json()).outcome).toBe("succeeded");
});

test("a rejected setting is reported without leaving a job behind", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  await page.getByTestId("file-input").setInputFiles(CUBE);
  await page.getByTestId("setting-layer_height").fill("not-a-number");
  await page.getByTestId("slice").click();

  // The worker rejects the value, so the job exists but fails with a stable code.
  await expect(page.getByTestId("job-state")).toHaveText("failed");
  await expect(page.getByTestId("job-error")).toBeVisible();
  await expect(page.getByTestId("download-gcode")).toHaveCount(0);

  await page.getByTestId("retry").click();
  await expect(page.getByTestId("retry-of")).toBeVisible();
});

test("an unsupported model is refused before a job is created", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  await page.getByTestId("file-input").setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("not a model"),
  });
  await page.getByTestId("slice").click();

  await expect(page.getByTestId("failure")).toContainText("unsupported_model_format");
  await expect(page.getByTestId("job-state")).toHaveCount(0);
});
