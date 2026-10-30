import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

const CUBE = resolve(dirname(fileURLToPath(import.meta.url)), "../../../tests/data/20mm_cube.obj");

async function prepareCube(page: Page) {
  await page.goto("/");
  await expect(page.getByTestId("filament-select")).toBeEnabled();
  await page.getByTestId("file-input").setInputFiles(CUBE);
  await expect(page.getByTestId("plate-item")).toHaveCount(1);
  await page.getByTestId("plate-item").click();
  await expect(page.getByTestId("object-tools")).toBeVisible();
}

/** Click the plate view at a fraction of its size; the centre falls on the cube's front face. */
async function clickPlate(page: Page, fx = 0.5, fy = 0.5) {
  const view = page.getByTestId("plater-view");
  const box = (await view.boundingBox())!;
  await page.mouse.click(box.x + box.width * fx, box.y + box.height * fy);
}

test("cutting a cube in half gives two half-height objects that slice", async ({ page }) => {
  await prepareCube(page);
  await page.getByTestId("tool-cut").click();
  await page.getByTestId("cut-height").fill("10");
  await page.getByTestId("cut-apply").click();

  await expect(page.getByTestId("tool-notice")).toHaveText("Cut into 2 parts.");
  const items = page.getByTestId("plate-item");
  await expect(items).toHaveCount(2);
  await expect(items.nth(0)).toContainText("20mm_cube (upper)");
  await expect(items.nth(1)).toContainText("20mm_cube (lower)");
  for (const index of [0, 1]) await expect(items.nth(index)).toContainText("20.0 × 20.0 × 10.0 mm");
  await expect(page.getByTestId("outside-warning")).toHaveCount(0);

  await page.getByTestId("slice").click();
  await expect(page.getByTestId("job-state")).toHaveText("succeeded");
  const jobId = await page.getByTestId("job-id").innerText();
  const index = await (await page.request.get(`/api/v1/jobs/${jobId}/artifacts/preview_index`)).json();
  expect(index.layers.at(-1).z).toBeLessThanOrEqual(10.01);
});

test("repairing a closed cube leaves it unchanged", async ({ page }) => {
  await prepareCube(page);
  await page.getByTestId("tool-repair").click();
  await expect(page.getByTestId("tool-notice")).toHaveText("The mesh had nothing to repair.");
  await expect(page.getByTestId("plate-item")).toHaveCount(1);
  await expect(page.getByTestId("plate-item")).toContainText("20mm_cube (repaired)");
  await expect(page.getByTestId("plate-item")).toContainText("20.0 × 20.0 × 20.0 mm");
});

test("laying a cube on its front face turns it a quarter about X", async ({ page }) => {
  await prepareCube(page);
  await page.getByTestId("tool-lay").click();
  await clickPlate(page);
  await expect(page.getByTestId("tool-lay")).toHaveAttribute("aria-pressed", "false");
  await expect.poll(async () => Math.abs(Number(await page.getByTestId("object-rotate-x").inputValue()))).toBeCloseTo(90, 3);
  await expect(page.getByTestId("plate-item")).toContainText("20.0 × 20.0 × 20.0 mm");
});

test("measuring reports the distance between two picked points", async ({ page }) => {
  await prepareCube(page);
  await page.getByTestId("tool-measure").click();
  await expect(page.getByTestId("measure-result")).toHaveText("Click two points on the plate's objects.");
  await clickPlate(page);
  await expect(page.getByTestId("measure-result")).toHaveText("Click a second point.");
  await clickPlate(page, 0.505, 0.5);
  await expect(page.getByTestId("measure-result")).toContainText(/^Distance \d+\.\d\d mm/);
  // Picking never changes the selection.
  await expect(page.getByTestId("object-panel")).toBeVisible();
});
