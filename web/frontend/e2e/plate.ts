/** Helpers shared by the end-to-end specs. */
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, type Page } from "@playwright/test";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
export const CUBE = resolve(REPO_ROOT, "tests/data/20mm_cube.obj");
// Two plates holding one cube each; see write_two_plate_project in
// tests/libslic3r/test_plate_artifacts.cpp, which writes the same project.
export const TWO_PLATES = resolve(REPO_ROOT, "tests/data/web-worker/two-plates.3mf");
for (const fixture of [CUBE, TWO_PLATES]) {
  if (!existsSync(fixture)) throw new Error(`The fixture ${fixture} is missing.`);
}
// Displayed and sliced placement must agree within this many millimetres; see
// docs/web/plater.md for why it is not zero.
export const PLACEMENT_TOLERANCE_MM = 0.5;

/** Wait for the profile lists the screen needs before it can submit anything. */
export async function waitForProfiles(page: Page) {
  await expect(page.getByTestId("printer-select")).toBeEnabled();
  await expect(page.getByTestId("process-select")).toBeEnabled();
  await expect(page.getByTestId("filament-select")).toBeEnabled();
}

export async function addCube(page: Page) {
  const before = await page.getByTestId("plate-item").count();
  await page.getByTestId("file-input").setInputFiles(CUBE);
  await expect(page.getByTestId("plate-item")).toHaveCount(before + 1);
}

/** Read every plate item's world bounds as the plater computed them. */
export async function plateBounds(page: Page) {
  const values = await page.getByTestId("plate-item").evaluateAll((items) =>
    items.map((item) => (item as HTMLElement).dataset.bounds!.split(",").map(Number)),
  );
  return values.map(([minX, minY, minZ, maxX, maxY, maxZ]) => ({ minX, minY, minZ, maxX, maxY, maxZ }));
}

/** Outer-wall extents of the sliced job, read from its paged preview. */
export async function slicedOuterWalls(page: Page, jobId: string) {
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

export async function sliceAndWait(page: Page) {
  await page.getByTestId("slice").click();
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");
  return page.getByTestId("job-id").innerText();
}
