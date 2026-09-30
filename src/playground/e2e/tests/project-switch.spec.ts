import { expect, test } from "@playwright/test";
import { themeRepos } from "../lib/fixtures";
import {
  addLocalProject,
  configureAndInitialize,
  firstScan,
  openMainSymbol,
  sidebarLink,
  themeOf,
} from "../lib/ui";

// Two scanned projects whose indexes hold different symbols: moving from one to
// the other must never show the first one's data (the SPA keys every query by
// slug and remounts the project subtree on a switch).
test("switching project shows the other project's data and none of the first's", async ({ page }, testInfo) => {
  const { notes, billing } = themeRepos(themeOf(testInfo));

  // The second project goes through the wizard too, this time by typing its path.
  await addLocalProject(page, billing, "path");
  await configureAndInitialize(page, billing);
  await firstScan(page, billing);

  // billing's Explorer, with one of its symbols selected.
  await sidebarLink(page, "Explorer").click();
  await openMainSymbol(page, billing);
  await expect(page.getByText(notes.mainSymbol)).toHaveCount(0);

  // Switch to notes through the sidebar's project switcher.
  await page.getByRole("button", { name: "Switch project" }).click();
  await page.getByRole("menuitem", { name: notes.name }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${notes.slug}$`));
  await expect(page.getByTestId("mcp-url")).toContainText(`/mcp/${notes.slug}`);
  await expect(page.getByText(billing.slug)).toHaveCount(0);

  // notes' Explorer starts clean: nothing selected, only notes' own tree.
  await sidebarLink(page, "Explorer").click();
  await expect(page).toHaveURL(new RegExp(`/p/${notes.slug}/explorer$`));
  await expect(page.getByText("Select a symbol to see its details.")).toBeVisible();
  await page.getByTestId("tree").getByText("src", { exact: true }).click();
  await expect(page.getByText(notes.fileName, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(billing.fileName)).toHaveCount(0);
  await expect(page.getByText(billing.mainSymbol)).toHaveCount(0);

  // A selection made in notes is notes' too, and switching back does not carry it over.
  await openMainSymbol(page, notes);
  await page.getByRole("button", { name: "Switch project" }).click();
  await page.getByRole("menuitem", { name: billing.name }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${billing.slug}$`));
  await sidebarLink(page, "Explorer").click();
  await expect(page.getByText("Select a symbol to see its details.")).toBeVisible();
  await expect(page.getByText(notes.mainSymbol)).toHaveCount(0);
  await expect(page.getByText(notes.qualifiedMain)).toHaveCount(0);
});
