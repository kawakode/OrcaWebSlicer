import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import type { Result as AxeViolation } from "axe-core";

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

// The tag set axe-core/Playwright's own docs recommend for an automated
// WCAG 2.1 A/AA pass.
const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

/** One line per violation, so a failure names the rule and the element rather
 *  than dumping axe's full nested result object. */
function describeViolations(violations: AxeViolation[]): string {
  return violations
    .map((violation) => {
      const targets = violation.nodes.map((node) => node.target.join(" ")).join(", ");
      return `${violation.id} (${violation.impact}): ${violation.help} — ${targets}`;
    })
    .join("\n");
}

async function expectNoViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
  expect(results.violations, describeViolations(results.violations)).toEqual([]);
}

test.describe("accessibility: automated axe pass", () => {
  test("the initial screen has no violations", async ({ page }) => {
    await page.goto("/");
    await waitForProfiles(page);
    await expectNoViolations(page);
  });

  test("a finished job and its layer preview have no violations", async ({ page }) => {
    await page.goto("/");
    await waitForProfiles(page);

    await page.getByTestId("file-input").setInputFiles(CUBE);
    await page.getByTestId("setting-layer_height").fill("0.3");
    await page.getByTestId("slice").click();
    await expect(page.getByTestId("job-state")).toHaveText("succeeded");
    await expect(page.getByTestId("preview-canvas")).toBeVisible();
    // The effective-configuration disclosure is part of the same screen.
    await page.getByTestId("job-report").locator("summary").click();

    await expectNoViolations(page);
  });

  test("a screen showing an error has no violations", async ({ page }) => {
    await page.goto("/");
    await waitForProfiles(page);

    await page.getByTestId("file-input").setInputFiles({
      name: "notes.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("not a model"),
    });
    await page.getByTestId("slice").click();
    await expect(page.getByTestId("failure")).toBeVisible();

    await expectNoViolations(page);
  });
});

test.describe("accessibility: keyboard navigation", () => {
  test("the supported flow is reachable and operable from the keyboard alone", async ({ page }) => {
    await page.goto("/");
    await waitForProfiles(page);

    // Repeatedly presses Tab, recording the data-testid of whatever gains
    // focus, until the target is reached. The recorded list doubles as proof
    // that everything in between was itself a real, reachable tab stop.
    async function tabUntil(targetTestId: string, maxSteps = 250): Promise<string[]> {
      const seen: string[] = [];
      for (let step = 0; step < maxSteps; step += 1) {
        const testId = await page.evaluate(
          () => (document.activeElement as HTMLElement | null)?.getAttribute("data-testid") ?? null,
        );
        if (testId) seen.push(testId);
        if (testId === targetTestId) return seen;
        await page.keyboard.press("Tab");
      }
      throw new Error(
        `Tab order never reached [data-testid="${targetTestId}"]; visited ${JSON.stringify(seen)}`,
      );
    }

    // The file input is first in the DOM and the flow's first stop.
    await tabUntil("file-input");
    await page.getByTestId("file-input").setInputFiles(CUBE);

    // Printer, process, and filament follow in the order the screen presents
    // them — each call only succeeds if the previous target was truly reached
    // first, so this also asserts forward focus order.
    await tabUntil("printer-select");
    await tabUntil("process-select");
    await tabUntil("filament-select");

    // The overrides form is generated from the engine's own settings; tabbing
    // to two curated settings that are guaranteed present (see slice.spec.ts)
    // proves the generated controls between them are real, reachable tab
    // stops too, and typing into them proves they're operable.
    await tabUntil("setting-layer_height");
    await page.keyboard.type("0.28");
    await expect(page.getByTestId("setting-layer_height")).toHaveValue("0.28");

    const toWallLoops = await tabUntil("setting-wall_loops");
    expect(toWallLoops.length).toBeGreaterThan(1);
    await page.keyboard.type("2");
    await expect(page.getByTestId("setting-wall_loops")).toHaveValue("2");

    // Slice is reached and activated with the keyboard, not a click.
    await tabUntil("slice");
    await page.keyboard.press("Enter");

    // Submitting disables the (still-focused) Slice button, which the browser
    // resolves by moving focus to <body> — correct behavior for a control
    // that genuinely stopped being interactive, not a trap. The next Tab
    // resumes at the top of the document, same as for any keyboard user.
    await expect(page.getByTestId("job-state")).toHaveText("succeeded");
    await expect(page.getByTestId("preview-canvas")).toBeVisible();

    // The layer preview's slider is reachable and operable with arrow keys,
    // exactly like any native range input.
    await tabUntil("preview-layer");
    const layers = Number(await page.getByTestId("preview-layer").getAttribute("max")) + 1;
    await expect(page.getByTestId("preview-layer-label")).toContainText(`${layers} / ${layers}`);
    await page.keyboard.press("ArrowLeft");
    await expect(page.getByTestId("preview-layer-label")).toContainText(`${layers - 1} / ${layers}`);
    await expect(page.getByTestId("preview-layer")).toHaveAttribute(
      "aria-valuetext",
      new RegExp(`Layer ${layers - 1} of ${layers}`),
    );

    // The travel toggle is a real checkbox: Space flips it like any native one.
    await tabUntil("preview-travels");
    await expect(page.getByTestId("preview-travels")).not.toBeChecked();
    await page.keyboard.press("Space");
    await expect(page.getByTestId("preview-travels")).toBeChecked();
  });
});
