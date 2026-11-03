import { expect, test } from "@playwright/test";

import { addCube, sliceAndWait, waitForProfiles } from "./plate";

test("an object on the second filament is printed with T1 and previewed in its colour", async ({ page }) => {
  await page.goto("/");
  await waitForProfiles(page);
  await addCube(page);
  await page.getByTestId("plate-item").click();
  await page.getByTestId("duplicate").click();
  await page.getByTestId("arrange").click();

  // A second filament, recoloured, opens the multi-material choices.
  await expect(page.getByTestId("multi-material")).toHaveCount(0);
  await page.getByTestId("add-filament").click();
  await expect(page.getByTestId("filament-slot")).toHaveCount(2);
  await page.getByTestId("filament-colour-2").fill("#0000ff");
  await expect(page.getByTestId("multi-material")).toBeVisible();

  await page.getByTestId("plate-item").nth(1).click();
  await page.getByTestId("object-filament").selectOption("2");
  await page.getByTestId("prime-tower").check();

  const jobId = await sliceAndWait(page);
  const job = await (await page.request.get(`/api/v1/jobs/${jobId}`)).json();
  expect(job.request.filaments).toHaveLength(2);
  expect(job.request.filaments[1].settings.filament_colour).toBe("#0000FF");
  expect(job.request.objects.map((placed: { extruder?: number }) => placed.extruder ?? 1).sort()).toEqual([1, 2]);
  expect(job.request.wipe_tower).toBeTruthy();

  const gcode = await (await page.request.get(`/api/v1/jobs/${jobId}/artifacts/gcode`)).text();
  expect(gcode).toMatch(/^T1\b/m);
  expect(gcode).toContain(";TYPE:Prime tower");

  // The preview opens coloured by filament and lists both.
  await expect(page.getByTestId("preview-view")).toBeVisible();
  await expect(page.getByTestId("preview-colour")).toHaveValue("filament");
  await expect(page.getByTestId("preview-tool")).toHaveCount(2);

  // Unloading the second filament moves its object back to the first.
  await page.getByTestId("tab-prepare").click();
  await page.getByTestId("filament-remove-2").click();
  await expect(page.getByTestId("filament-slot")).toHaveCount(1);
  await expect(page.getByTestId("unloaded-filament-warning")).toHaveCount(0);
  await expect(page.getByTestId("slice")).toBeEnabled();
});
