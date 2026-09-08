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
  // Wait for the transfer itself, not just the event that started it: the
  // assertion above passes even if no byte ever lands.
  expect(await download.failure()).toBeNull();
  expect(await download.path()).toBeTruthy();

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

test("the overrides form is generated from the engine's own definitions", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  // The bounds and the options are the engine's; nothing here is restated in
  // the app, so an engine change is visible without a frontend change.
  const layerHeight = page.getByTestId("setting-layer_height");
  await expect(layerHeight).toHaveAttribute("type", "number");
  await expect(layerHeight).toHaveAttribute("placeholder", /profile default/);

  // `wall_loops` is one the engine does bound, so its range reaches the control
  // while `layer_height`, which the engine leaves open, carries none.
  const wallLoops = page.getByTestId("setting-wall_loops");
  expect(Number(await wallLoops.getAttribute("max"))).toBeGreaterThan(0);
  expect(await layerHeight.getAttribute("max")).toBeNull();

  const supportType = page.getByTestId("setting-support_type");
  await expect(supportType).toHaveJSProperty("tagName", "SELECT");
  expect(await supportType.locator("option").count()).toBeGreaterThan(2);

  // A gated setting is only disabled once its gate is explicitly switched off.
  await expect(supportType).toBeEnabled();
  await page.getByTestId("setting-enable_support").selectOption("0");
  await expect(supportType).toBeDisabled();
});

test("an override the engine cannot accept is refused before a job is created", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  await page.getByTestId("file-input").setInputFiles(CUBE);
  // The engine bounds this one at 1000, so the API refuses it from the same
  // definition the control was generated from.
  await page.getByTestId("setting-wall_loops").fill("5000");
  await page.getByTestId("slice").click();

  await expect(page.getByTestId("failure")).toContainText("invalid_setting_value");
  await expect(page.getByTestId("job-state")).toHaveCount(0);
});

test("the job report names the profile chain and the overrides", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  await page.getByTestId("file-input").setInputFiles(CUBE);
  await page.getByTestId("setting-layer_height").fill("0.28");
  await page.getByTestId("slice").click();
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");

  await page.getByTestId("job-report").locator("summary").click();
  await expect(page.getByTestId("job-profile-process")).toContainText("0.20mm");
  await expect(page.getByTestId("job-overrides")).toContainText("0.28");

  await page.getByTestId("retry").click();
  await expect(page.getByTestId("retry-of")).toBeVisible();
});

test("the layer preview steps through the layers the job produced", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  await page.getByTestId("file-input").setInputFiles(CUBE);
  await page.getByTestId("setting-layer_height").fill("0.3");
  await page.getByTestId("slice").click();
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");

  await expect(page.getByTestId("preview-canvas")).toBeVisible();
  const slider = page.getByTestId("preview-layer");
  const layers = Number(await slider.getAttribute("max")) + 1;
  expect(layers).toBeGreaterThan(40);

  // The panel opens on the top layer and moves without refetching the print.
  await expect(page.getByTestId("preview-layer-label")).toContainText(`${layers} / ${layers}`);
  await slider.fill("0");
  await expect(page.getByTestId("preview-layer-label")).toContainText(`1 / ${layers}`);
  await expect(page.getByTestId("preview-legend")).toContainText("wall");

  // The index agrees with the G-code the same job published.
  const jobId = await page.getByTestId("job-id").innerText();
  const index = await (await page.request.get(`/api/v1/jobs/${jobId}/preview`)).json();
  expect(index.layers.length).toBe(layers);
  const gcode = await (await page.request.get(`/api/v1/jobs/${jobId}/artifacts/gcode`)).text();
  expect(gcode.split(";LAYER_CHANGE").length - 1).toBe(layers);

  // One layer's request returns only that layer's bytes.
  const first = await page.request.get(`/api/v1/jobs/${jobId}/preview/layers/0`);
  expect(first.ok()).toBeTruthy();
  expect((await first.body()).byteLength).toBe(index.layers[0].length);
  expect(index.layers[0].length).toBeLessThan(index.data_bytes);
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
