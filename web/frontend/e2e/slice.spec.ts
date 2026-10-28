import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const CUBE = resolve(REPO_ROOT, "tests/data/20mm_cube.obj");
// Two plates holding one cube each; see write_two_plate_project in
// tests/libslic3r/test_plate_artifacts.cpp, which writes the same project.
const TWO_PLATES = resolve(REPO_ROOT, "tests/data/web-worker/two-plates.3mf");
// Displayed and sliced placement must agree within this many millimetres; see
// docs/web/plater.md for why it is not zero.
const PLACEMENT_TOLERANCE_MM = 0.5;

test.beforeAll(() => {
  for (const fixture of [CUBE, TWO_PLATES]) {
    if (!existsSync(fixture)) throw new Error(`The fixture ${fixture} is missing.`);
  }
});

/** Wait for the profile lists the screen needs before it can submit anything. */
async function waitForProfiles(page: Page) {
  await expect(page.getByTestId("printer-select")).toBeEnabled();
  await expect(page.getByTestId("process-select")).toBeEnabled();
  await expect(page.getByTestId("filament-select")).toBeEnabled();
}

async function addCube(page: Page) {
  const before = await page.getByTestId("plate-item").count();
  await page.getByTestId("file-input").setInputFiles(CUBE);
  await expect(page.getByTestId("plate-item")).toHaveCount(before + 1);
}

/** Read every plate item's world bounds as the plater computed them. */
async function plateBounds(page: Page) {
  const values = await page.getByTestId("plate-item").evaluateAll((items) =>
    items.map((item) => (item as HTMLElement).dataset.bounds!.split(",").map(Number)),
  );
  return values.map(([minX, minY, minZ, maxX, maxY, maxZ]) => ({ minX, minY, minZ, maxX, maxY, maxZ }));
}

/** Outer-wall extents of the sliced job, read from its paged preview. */
async function slicedOuterWalls(page: Page, jobId: string) {
  const index = await (await page.request.get(`/api/v1/jobs/${jobId}/artifacts/preview_index`)).json();
  const outer = index.roles.find((role: { name: string }) => role.name === "Outer wall").id;
  const last = index.layers.length - 1;
  const body = await (await page.request.get(`/api/v1/jobs/${jobId}/preview/layers?start=0&end=${last}`)).body();
  const view = new DataView(body.buffer, body.byteOffset, body.byteLength);
  const points: [number, number, number][] = [];
  for (let at = 0; at + 36 <= body.byteLength; at += 36) {
    if (view.getUint8(at + 32) !== outer) continue;
    points.push([view.getFloat32(at, true), view.getFloat32(at + 4, true), view.getFloat32(at + 8, true)]);
    points.push([view.getFloat32(at + 12, true), view.getFloat32(at + 16, true), view.getFloat32(at + 20, true)]);
  }
  return { index, points };
}

async function sliceAndWait(page: Page) {
  await page.getByTestId("slice").click();
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");
  return page.getByTestId("job-id").innerText();
}

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
  // A plate's G-code is named after its job and its plate.
  expect(download.suggestedFilename()).toMatch(/^job-[0-9a-f]{32}-plate-1\.gcode$/);

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

/** Count the outer-wall points of a sliced job that fall inside a displayed footprint. */
function wallsInside(
  points: [number, number, number][],
  box: { minX: number; maxX: number; minY: number; maxY: number },
) {
  return points.filter(
    ([x, y]) =>
      x >= box.minX - PLACEMENT_TOLERANCE_MM &&
      x <= box.maxX + PLACEMENT_TOLERANCE_MM &&
      y >= box.minY - PLACEMENT_TOLERANCE_MM &&
      y <= box.maxY + PLACEMENT_TOLERANCE_MM,
  ).length;
}

test("objects move between plates and every plate slices on its own", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("add-plate").click();
  await expect(page.getByTestId("plate-tab")).toHaveCount(2);
  await expect(page.getByTestId("plate-tab").nth(1)).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("plate-item")).toHaveCount(0);
  await addCube(page);
  await addCube(page);

  // An object moves to another plate at the same place on that plate's bed.
  await page.getByTestId("plate-item").first().click();
  const [moving] = await plateBounds(page);
  await page.getByTestId("object-plate").selectOption({ label: "Plate 1" });
  await expect(page.getByTestId("plate-item")).toHaveCount(1);
  await page.getByTestId("plate-tab").first().click();
  await expect(page.getByTestId("plate-item")).toHaveCount(2);
  expect((await plateBounds(page))[1]).toEqual(moving);
  await page.getByTestId("arrange").click();
  const first = await plateBounds(page);

  await page.getByTestId("plate-tab").nth(1).click();
  const second = await plateBounds(page);
  expect(second).toHaveLength(1);

  // One batch slices both plates, one job each.
  await page.getByTestId("slice-all").click();
  await expect(page.getByTestId("plate-status").nth(0)).toHaveAttribute("data-state", "succeeded");
  await expect(page.getByTestId("plate-status").nth(1)).toHaveAttribute("data-state", "succeeded");
  await expect(page.getByTestId("job-plate")).toHaveText("Plate 2:");

  // Each plate's preview holds that plate's objects and nothing else.
  const secondJob = await page.getByTestId("job-id").innerText();
  await expect(page.getByTestId("preview-view")).toBeVisible();
  const secondWalls = (await slicedOuterWalls(page, secondJob)).points;
  expect(wallsInside(secondWalls, second[0])).toBe(secondWalls.length);

  await page.getByTestId("plate-tab").first().click();
  await page.getByTestId("tab-preview").click();
  await expect(page.getByTestId("preview-view")).toBeVisible();
  const firstJob = await page.getByTestId("job-id").innerText();
  expect(firstJob).not.toBe(secondJob);
  const firstWalls = (await slicedOuterWalls(page, firstJob)).points;
  for (const box of first) expect(wallsInside(firstWalls, box)).toBeGreaterThan(100);

  const download = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("plate-download").nth(1).click(),
  ]).then(([event]) => event);
  expect(download.suggestedFilename()).toBe(`${secondJob}-plate-2.gcode`);

  // A plate is also sliced on its own.
  await page.getByTestId("tab-prepare").click();
  await page.getByTestId("slice").click();
  await expect(page.getByTestId("job-id")).not.toHaveText(firstJob);
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");
});

test("a project keeps its plates and their placements", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await page.getByTestId("file-input").setInputFiles(TWO_PLATES);

  await expect(page.getByTestId("plate-tab")).toHaveCount(2);
  await expect(page.getByTestId("plate-tab").first()).toContainText("First");
  await expect(page.getByTestId("plate-tab").nth(1)).toContainText("Second");
  await expect(page.getByTestId("plate-item")).toHaveCount(1);
  const [first] = await plateBounds(page);
  expect(first.minX).toBeCloseTo(90, 3);

  // The second plate's cube sits where it does on that plate's own bed.
  await page.getByTestId("plate-tab").nth(1).click();
  await expect(page.getByTestId("plate-item")).toHaveCount(1);
  const [second] = await plateBounds(page);
  expect(second.minX).toBeCloseTo(50, 3);
  expect(second.minY).toBeCloseTo(70, 3);

  await page.getByTestId("slice").click();
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");
  const jobId = await page.getByTestId("job-id").innerText();
  const { points } = await slicedOuterWalls(page, jobId);
  expect(wallsInside(points, second)).toBe(points.length);
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
