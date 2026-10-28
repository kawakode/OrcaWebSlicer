import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import {
  PLACEMENT_TOLERANCE_MM,
  addCube,
  plateBounds,
  sliceAndWait,
  slicedOuterWalls,
  waitForProfiles,
} from "./plate";

test("an uploaded model shows its bounds and slices where it was placed", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);

  // The worker-reported geometry gives the 20 mm cube its true size.
  await expect(page.getByTestId("plate-item")).toContainText("20.0 × 20.0 × 20.0 mm");

  await page.getByTestId("plate-item").click();
  await page.getByTestId("object-x").fill("60");
  await page.getByTestId("object-x").press("Enter");
  await page.getByTestId("object-y").fill("70");
  await page.getByTestId("object-y").press("Enter");
  const [shown] = await plateBounds(page);
  expect(shown.minX).toBeCloseTo(50, 3);
  expect(shown.minY).toBeCloseTo(60, 3);

  await page.getByTestId("setting-layer_height").fill("0.28");
  const jobId = await sliceAndWait(page);
  await expect(page.getByTestId("failure")).toHaveCount(0);

  // The preview opens on its own and agrees with the G-code.
  await expect(page.getByTestId("preview-view")).toBeVisible();
  const gcode = await (await page.request.get(`/api/v1/jobs/${jobId}/artifacts/gcode`)).text();
  const layerChanges = gcode.split(";LAYER_CHANGE").length - 1;
  await expect(page.getByTestId("preview-layer-count")).toHaveText(String(layerChanges));
  expect(layerChanges).toBeGreaterThan(40);

  const { index, points } = await slicedOuterWalls(page, jobId);
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  expect(Math.abs(Math.min(...xs) - shown.minX)).toBeLessThan(PLACEMENT_TOLERANCE_MM);
  expect(Math.abs(Math.max(...xs) - shown.maxX)).toBeLessThan(PLACEMENT_TOLERANCE_MM);
  expect(Math.abs(Math.min(...ys) - shown.minY)).toBeLessThan(PLACEMENT_TOLERANCE_MM);
  expect(Math.abs(Math.max(...ys) - shown.maxY)).toBeLessThan(PLACEMENT_TOLERANCE_MM);
  // The preview's Z range is the G-code's layer range; Z hops are not layers.
  const zs = [...gcode.matchAll(/^;Z:([\d.]+)/gm)].map((match) => Number(match[1]));
  expect(index.layers[0].z).toBeCloseTo(zs[0], 2);
  expect(index.layers.at(-1).z).toBeCloseTo(zs.at(-1)!, 2);

  await page.getByTestId("preview-slider").fill("0");
  await expect(page.getByTestId("preview-layer")).toHaveText("1");

  const download = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("download-gcode").click(),
  ]).then(([event]) => event);
  expect(download.suggestedFilename()).toMatch(/^job-[0-9a-f]{32}\.gcode$/);

  // The job names the whole effective chain and the override it applied.
  await expect(page.getByTestId("job-chain-process")).toContainText("fdm_process_common");
  await expect(page.getByTestId("job-overrides")).toContainText("layer_height = 0.28");

  await page.getByTestId("retry").click();
  await expect(page.getByTestId("retry-of")).toContainText(jobId);
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");
});

test("duplicated and arranged objects are all sliced", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("plate-item").click();
  await page.getByTestId("duplicate").click();
  await addCube(page);
  await page.getByTestId("arrange").click();

  const shown = await plateBounds(page);
  expect(shown).toHaveLength(3);
  const jobId = await sliceAndWait(page);
  const { points } = await slicedOuterWalls(page, jobId);
  // Every displayed footprint has outer walls inside it.
  for (const box of shown) {
    const inside = points.filter(
      ([x, y]) =>
        x >= box.minX - PLACEMENT_TOLERANCE_MM && x <= box.maxX + PLACEMENT_TOLERANCE_MM &&
        y >= box.minY - PLACEMENT_TOLERANCE_MM && y <= box.maxY + PLACEMENT_TOLERANCE_MM,
    );
    expect(inside.length).toBeGreaterThan(100);
  }
});

test("the plate can be edited from the keyboard", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("plate-item").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("object-panel")).toBeVisible();
  const [before] = await plateBounds(page);

  await page.getByLabel("Plate workspace").focus();
  await page.keyboard.press("Shift+ArrowRight");
  const [after] = await plateBounds(page);
  expect(after.minX - before.minX).toBeCloseTo(10, 3);

  await page.keyboard.press("Control+d");
  await expect(page.getByTestId("plate-item")).toHaveCount(2);
  await page.keyboard.press("Delete");
  await expect(page.getByTestId("plate-item")).toHaveCount(1);
});

test("an object outside the printable volume blocks slicing", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("plate-item").click();
  await page.getByTestId("object-x").fill("-50");
  await page.getByTestId("object-x").press("Enter");
  await expect(page.getByTestId("outside-warning")).toBeVisible();
  await expect(page.getByTestId("slice")).toBeDisabled();
});

test("the settings form is generated from the engine catalog", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  // Every curated group the engine exported is rendered, with the selected
  // profile's own value as what a blank override keeps.
  await expect(page.getByTestId("settings-group-quality")).toBeVisible();
  await expect(page.getByTestId("setting-layer_height")).toHaveAttribute("placeholder", /^0\.\d+$/);
  // A dependent setting is disabled until the setting that enables it is on.
  await expect(page.getByTestId("setting-support_type")).toBeDisabled();
  await page.getByTestId("setting-enable_support").selectOption("1");
  await expect(page.getByTestId("setting-support_type")).toBeEnabled();
});

test("a rejected setting is reported without leaving a job behind", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("setting-layer_height").fill("not-a-number");
  await page.getByTestId("slice").click();

  // The engine's own definition refuses the value before a worker is started.
  await expect(page.getByTestId("failure")).toContainText("invalid_setting_value");
  // The refusal names the setting, and its control is marked for correction.
  await expect(page.getByTestId("setting-layer_height")).toHaveAttribute("aria-invalid", "true");
  await expect(page.getByTestId("job-state")).toHaveCount(0);
  await expect(page.getByTestId("download-gcode")).toHaveCount(0);
});

test("an unsupported model is refused before it reaches the plate", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  await page.getByTestId("file-input").setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("not a model"),
  });

  await expect(page.getByTestId("failure")).toContainText("unsupported_model_format");
  await expect(page.getByTestId("plate-item")).toHaveCount(0);
  await expect(page.getByTestId("slice")).toBeDisabled();
});

test("an unreadable model reports the worker's stable error", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);

  await page.getByTestId("file-input").setInputFiles({
    name: "broken.stl",
    mimeType: "model/stl",
    buffer: Buffer.from("solid broken\nthis is not geometry\nendsolid broken\n"),
  });

  await expect(page.getByTestId("failure")).toContainText("model_load_failed");
  await expect(page.getByTestId("plate-item")).toHaveCount(0);
});

test("the prepared plate has no serious accessibility violations", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("plate-item").click();

  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations.filter((violation) => ["serious", "critical"].includes(violation.impact ?? ""));
  expect(serious.map((violation) => `${violation.id}: ${violation.help}`)).toEqual([]);
});
