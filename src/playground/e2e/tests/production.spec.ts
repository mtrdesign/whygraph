import { expect, test } from "@playwright/test";

// Placeholder until the production flow (bootstrap, orgs, sign-in) is written
// against the SPA: only proves the production portal serves the app on its base URL.
test("the production portal serves the SPA", async ({ page }) => {
  const res = await page.goto("/");
  expect(res?.status()).toBe(200);
  await expect(page.locator("#root")).toBeAttached();
});
