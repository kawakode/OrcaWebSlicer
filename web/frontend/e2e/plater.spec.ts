import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const CUBE = resolve(REPO_ROOT, "tests/data/20mm_cube.obj");

/**
 * How far the printed outline may sit from the placed model's own bounding
 * box, once the skirt and brim are switched off so nothing else surrounds the
 * part. The outer wall's centreline is inset from the mesh surface by half an
 * extrusion width — measured at 0.20 mm for a 0.4 mm wall by
 * `scripts/test_web_placement.py` — and this leaves room for the widest nozzle
 * the bundled profiles ship with.
 *
 * docs/web/mvp.md acceptance criterion 2 is what this documents.
 */
const PLACEMENT_TOLERANCE_MM = 1;
/** A pure translation must move the print by exactly that much. */
const TRANSLATION_TOLERANCE_MM = 0.05;

test.beforeAll(() => {
  if (!existsSync(CUBE)) throw new Error(`The fixture ${CUBE} is missing.`);
});

async function waitForPlate(page: Page) {
  await expect(page.getByTestId("printer-select")).toBeEnabled();
  await expect(page.getByTestId("process-select")).toBeEnabled();
  await expect(page.getByTestId("filament-select")).toBeEnabled();
  await page.getByTestId("file-input").setInputFiles(CUBE);
  await expect(page.getByTestId("plater-canvas")).toBeVisible();
}

/**
 * The XY box of the model's own toolpath.
 *
 * The machine's start and end G-code are cut away first: a printer profile is
 * free to draw a purge line across the bed or park at a fixed coordinate, and
 * neither moves with the object, so either would swamp the measurement. Both
 * blocks are wrapped in `;TYPE:Custom`, and the first real layer always opens
 * with `;LAYER_CHANGE`.
 */
function extrudedBounds(gcode: string) {
  const lines = gcode.split("\n");
  const first = lines.findIndex((line) => line.startsWith(";LAYER_CHANGE"));
  const last = lines.map((line) => line.startsWith(";TYPE:Custom")).lastIndexOf(true);
  const model = lines.slice(first < 0 ? 0 : first, last > first ? last : lines.length);

  let x = 0;
  let y = 0;
  let e = 0;
  // The mode is set in the start G-code, which was just cut away.
  let relativeE = /^\s*M83\b/m.test(gcode);
  const bounds = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  const include = (px: number, py: number) => {
    bounds.minX = Math.min(bounds.minX, px);
    bounds.minY = Math.min(bounds.minY, py);
    bounds.maxX = Math.max(bounds.maxX, px);
    bounds.maxY = Math.max(bounds.maxY, py);
  };

  for (const raw of model) {
    const line = raw.split(";", 1)[0].trim();
    if (line === "M83") relativeE = true;
    else if (line === "M82") relativeE = false;
    if (!/^G[01](\s|$)/.test(line)) continue;
    const words = new Map(
      [...line.matchAll(/([A-Za-z])(-?\d*\.?\d+)/g)].map((word) => [word[1].toUpperCase(), Number(word[2])]),
    );
    const nextX = words.get("X") ?? x;
    const nextY = words.get("Y") ?? y;
    const extrusion = words.get("E");
    // Travels, retractions, and the parking moves in the end G-code are not
    // part of the printed object, so only extruding moves set the bounds.
    if (extrusion !== undefined && (relativeE ? extrusion > 0 : extrusion > e)) {
      include(x, y);
      include(nextX, nextY);
    }
    if (extrusion !== undefined) e = relativeE ? e + extrusion : extrusion;
    x = nextX;
    y = nextY;
  }
  return bounds;
}

/** Slice the current plate and return the finished job's id. */
async function slice(page: Page, previousJobId?: string): Promise<string> {
  await page.getByTestId("slice").click();
  // The previous job is still on screen while this one is being accepted, so
  // waiting for "succeeded" alone would match the job that already finished.
  if (previousJobId) await expect(page.getByTestId("job-id")).not.toHaveText(previousJobId);
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");
  return page.getByTestId("job-id").innerText();
}

/** The bed the selected printer declares, read off the plate's own summary. */
async function bedSize(page: Page): Promise<[number, number]> {
  const summary = await page.getByTestId("plater-summary").innerText();
  const measured = summary.match(/(\d+(?:\.\d+)?) × (\d+(?:\.\d+)?) mm bed/);
  if (!measured) throw new Error(`The plate summary named no bed: ${summary}`);
  return [Number(measured[1]), Number(measured[2])];
}

test("the plate shows the uploaded model on the configured bed", async ({ page }) => {
  await page.goto("/");
  await waitForPlate(page);

  // The bed is whichever printer the screen defaulted to, so it is read rather
  // than assumed; the bounds are the model's own, in millimetres.
  await expect(page.getByTestId("plater-summary")).toContainText("1 object");
  await expect(page.getByTestId("plater-bounds")).toHaveText("20.00 × 20.00 × 20.00 mm");
  await expect(page.getByTestId("plater-outside-bed")).toHaveCount(0);

  // A freshly loaded plate is arranged, so the 20 mm cube sits centred.
  const [width, depth] = await bedSize(page);
  expect(Number(await page.getByTestId("plater-x").inputValue())).toBeCloseTo(width / 2 - 10, 1);
  expect(Number(await page.getByTestId("plater-y").inputValue())).toBeCloseTo(depth / 2 - 10, 1);
});

test("objects can be duplicated, arranged, and deleted", async ({ page }) => {
  await page.goto("/");
  await waitForPlate(page);

  await page.getByTestId("plater-duplicate").click();
  await expect(page.getByTestId("plater-summary")).toContainText("2 objects");
  await expect(page.getByTestId("plater-objects").locator("button")).toHaveCount(2);

  // Arranging keeps both copies apart rather than stacking them.
  await page.getByTestId("plater-arrange").click();
  await page.getByTestId("plater-select-0").click();
  const firstX = Number(await page.getByTestId("plater-x").inputValue());
  await page.getByTestId("plater-select-1").click();
  const secondX = Number(await page.getByTestId("plater-x").inputValue());
  expect(Math.abs(secondX - firstX)).toBeGreaterThanOrEqual(20);

  await page.getByTestId("plater-delete").click();
  await page.getByTestId("plater-select-0").click();
  await page.getByTestId("plater-delete").click();
  await expect(page.getByTestId("plater-summary")).toContainText("0 objects");

  // An empty plate has nothing to slice, and says so rather than only dimming.
  await expect(page.getByTestId("slice")).toBeDisabled();
  await expect(page.locator("#slice-hint")).toContainText("plate is empty");
});

test("rotation and uniform scale change the placed size", async ({ page }) => {
  await page.goto("/");
  await waitForPlate(page);

  await page.getByTestId("plater-scale").fill("50");
  await expect(page.getByTestId("plater-bounds")).toHaveText("10.00 × 10.00 × 10.00 mm");

  // A 45° turn is the one that proves the rotation reached the geometry: the
  // cube's own footprint is square, so its axis-aligned box only grows off-axis.
  await page.getByTestId("plater-rotation").fill("45");
  await expect(page.getByTestId("plater-bounds")).toHaveText("14.14 × 14.14 × 10.00 mm");
});

test("the placement the plater shows is the placement that gets sliced", async ({ page }) => {
  await page.goto("/");
  await waitForPlate(page);

  // Nothing but the object itself may touch the bed, or the printed outline
  // would be the skirt's rather than the model's.
  await page.getByTestId("setting-skirt_loops").fill("0");
  await page.getByTestId("setting-brim_type").selectOption("no_brim");

  await page.getByTestId("plater-x").fill("60");
  await page.getByTestId("plater-y").fill("40");
  const first = await slice(page);

  // The transform the browser displayed is the one the API recorded, in the
  // column-major millimetre layout docs/web/scene-format.md publishes.
  const job = await (await page.request.get(`/api/v1/jobs/${first}`)).json();
  expect(job.request.objects).toHaveLength(1);
  expect(job.request.objects[0].source_object).toBe(0);
  expect(job.request.objects[0].transform[12]).toBeCloseTo(60, 3);
  expect(job.request.objects[0].transform[13]).toBeCloseTo(40, 3);

  // Two-sided: the outline may sit inside the model's box by half a wall, but
  // it may not sit anywhere else.
  const before = extrudedBounds(await (await page.request.get(`/api/v1/jobs/${first}/artifacts/gcode`)).text());
  for (const [measured, placed] of [
    [before.minX, 60],
    [before.maxX, 80],
    [before.minY, 40],
    [before.maxY, 60],
  ]) {
    expect(Math.abs(measured - placed)).toBeLessThan(PLACEMENT_TOLERANCE_MM);
  }

  // A pure translation is the exact check: every extrusion-width effect cancels.
  await page.getByTestId("plater-x").fill("100");
  const second = await slice(page, first);
  const after = extrudedBounds(await (await page.request.get(`/api/v1/jobs/${second}/artifacts/gcode`)).text());
  expect(after.minX - before.minX).toBeCloseTo(40, 1);
  expect(after.maxX - before.maxX).toBeCloseTo(40, 1);
  expect(Math.abs(after.minY - before.minY)).toBeLessThan(TRANSLATION_TOLERANCE_MM);
});
