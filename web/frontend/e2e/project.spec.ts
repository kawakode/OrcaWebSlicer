import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

import { PLACEMENT_TOLERANCE_MM, addCube, plateBounds, sliceAndWait, slicedOuterWalls, waitForProfiles } from "./plate";

test("a saved project reopens with its placement, profiles, and overrides", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("plate-item").click();
  await page.getByTestId("object-x").fill("60");
  await page.getByTestId("object-x").press("Enter");
  await page.getByTestId("object-y").fill("70");
  await page.getByTestId("object-y").press("Enter");
  // A process other than the printer's default proves the selection is restored.
  if ((await page.getByTestId("process-select").locator("option").count()) > 1) {
    await page.getByTestId("process-select").selectOption({ index: 1 });
  }
  await page.getByTestId("setting-layer_height").fill("0.28");
  const process = await page.getByTestId("process-select").inputValue();
  const [placed] = await plateBounds(page);

  const download = await Promise.all([page.waitForEvent("download"), page.getByTestId("save-project").click()]).then(
    ([event]) => event,
  );
  expect(download.suggestedFilename()).toMatch(/^job-[0-9a-f]{32}\.3mf$/);
  const saved = join(mkdtempSync(join(tmpdir(), "orca-project-")), "plate.3mf");
  await download.saveAs(saved);
  // A desktop project is a ZIP archive.
  expect(readFileSync(saved).subarray(0, 4).toString("binary")).toBe("PK\x03\x04");
  await expect(page.getByTestId("download-project")).toBeVisible();

  // A fresh session opens the project on an empty plate.
  await page.goto("/");
  await waitForProfiles(page);
  await page.getByTestId("file-input").setInputFiles(saved);
  await expect(page.getByTestId("plate-item")).toHaveCount(1);
  await expect(page.getByTestId("notice")).toContainText("Restored the project's profiles");
  await expect(page.getByTestId("setting-layer_height")).toHaveValue("0.28");
  await expect(page.getByTestId("process-select")).toHaveValue(process);
  const [reopened] = await plateBounds(page);
  for (const key of ["minX", "minY", "maxX", "maxY"] as const) expect(reopened[key]).toBeCloseTo(placed[key], 3);

  // The reopened plate slices where it was saved, with a G-code thumbnail.
  await expect(page.getByTestId("gcode-thumbnails")).toBeChecked();
  const jobId = await sliceAndWait(page);
  const { points } = await slicedOuterWalls(page, jobId);
  const xs = points.map(([x]) => x);
  expect(Math.abs(Math.min(...xs) - placed.minX)).toBeLessThan(PLACEMENT_TOLERANCE_MM);
  expect(Math.abs(Math.max(...xs) - placed.maxX)).toBeLessThan(PLACEMENT_TOLERANCE_MM);
  const gcode = await (await page.request.get(`/api/v1/jobs/${jobId}/artifacts/gcode`)).text();
  expect(gcode).toMatch(/^; thumbnail begin \d+x\d+/m);
});

test("a project added to an existing plate keeps the current settings", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("setting-layer_height").fill("0.12");
  const download = await Promise.all([page.waitForEvent("download"), page.getByTestId("save-project").click()]).then(
    ([event]) => event,
  );
  const saved = join(mkdtempSync(join(tmpdir(), "orca-project-")), "plate.3mf");
  await download.saveAs(saved);

  await page.getByTestId("setting-layer_height").fill("0.24");
  await page.getByTestId("file-input").setInputFiles(saved);
  await expect(page.getByTestId("plate-item")).toHaveCount(2);
  await expect(page.getByTestId("setting-layer_height")).toHaveValue("0.24");
  await expect(page.getByTestId("notice")).toHaveCount(0);
});
