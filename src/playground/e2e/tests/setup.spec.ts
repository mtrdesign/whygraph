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
  await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
  // The empty state is the first-run checklist: add a key, add a project, connect an agent.
  await expect(page.getByText("No projects yet")).toBeVisible();
  const checklist = page.getByTestId("first-run-checklist");
  await expect(checklist.getByTestId("first-run-list").getByRole("listitem")).toHaveCount(3);
  await expect(checklist.getByRole("link", { name: "Add a key" })).toBeVisible();
  await expect(checklist.getByRole("link", { name: "Add project" })).toBeVisible();
  await expect(checklist.getByTestId("first-run-agent")).toContainText("Connect your agent");
  await expect(page.locator('[data-testid^="project-"]')).toHaveCount(0);

  // Setup is one-way: the gate now keeps /setup closed.
  await page.goto("/setup");
  await expect(page).not.toHaveURL(/\/setup$/);
});
