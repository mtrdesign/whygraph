import { expect, test } from "@playwright/test";

// Screen 1: a fresh portal sends every URL to /setup until the one local user
// exists. Runs once (the `setup` project); the colour-scheme passes depend on it.
test("first run: the portal asks for a name, then shows an empty projects page", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole("heading", { name: "Welcome to WhyGraph" })).toBeVisible();

  await page.getByLabel("Your name").fill("Ada Lovelace");
  await page.getByRole("button", { name: "Continue" }).click();

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
  await expect(page.getByText("No projects yet")).toBeVisible();
  await expect(page.locator('[data-testid^="project-"]')).toHaveCount(0);

  // Setup is one-way: the gate now keeps /setup closed.
  await page.goto("/setup");
  await expect(page).not.toHaveURL(/\/setup$/);
});
