// End-to-end smoke test on the SYNTHETIC fixture (not real AIS): select vessel -> set cutoff ->
// run baseline -> overlay truth -> view error (build plan §7 Milestone 5).
// Vessel 999000001 moves in a straight line at 12 kn, COG 45°, reporting every minute from
// 00:00 to 02:00 UTC on 2023-06-01, so a 00:30 cutoff has truth at every 1 h target time:
// persistence misses by 12 kn x 1 h = 22.2 km and constant velocity by almost nothing.
import { expect, test } from "@playwright/test";

test("forecast a fixture vessel from a cutoff and overlay the withheld truth", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByLabel("Dataset")).toHaveValue("SYNTHETIC_fixture");

  // Select the vessel.
  await page.getByTestId("vessel-list").getByRole("button", { name: /999000001/ }).click();
  await expect(page.getByTestId("track-summary")).toContainText("121 reports");
  await expect(page.getByTestId("last-report")).toContainText("2023-06-01T02:00:00Z · 12 kn · COG 45°");

  // Set the cutoff: report 31 of 121 is 00:30:00Z. Reports after it leave the map.
  await page.getByTestId("cutoff-toggle").check();
  await page.getByTestId("cutoff-slider").fill("30");
  await expect(page.getByText("2023-06-01T00:30:00Z")).toBeVisible();

  // Run the baselines only, at 1 h.
  const panel = page.getByTestId("forecast-panel");
  await panel.getByLabel("Horizon").selectOption("1");
  for (const box of await panel.getByRole("checkbox").all()) await box.uncheck();
  await panel.getByRole("checkbox", { name: "persistence_v1" }).check();
  await panel.getByRole("checkbox", { name: "constant_velocity_v1" }).check();
  await expect(page.getByTestId("show-truth")).toHaveCount(0); // no truth before a forecast
  await page.getByTestId("run-forecast").click();
  await expect(page.getByTestId("run-forecast")).toHaveText("Forecast is current");

  // Overlay the withheld truth and read the error table.
  await page.getByTestId("show-truth").click();
  const table = page.getByTestId("evaluation");
  await expect(table).toBeVisible();
  const cell = (model: string) => table.getByRole("row", { name: new RegExp(`^${model}`) }).getByRole("cell").nth(1);
  expect(Number(await cell("persistence").innerText())).toBeCloseTo(22.2, 1);
  expect(Number(await cell("constant_velocity").innerText())).toBeLessThan(0.1);
  await expect(table.getByRole("row", { name: /^truth/ })).toContainText("✓");
});
