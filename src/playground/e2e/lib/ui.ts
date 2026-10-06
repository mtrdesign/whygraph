import { expect, type Locator, type Page, type TestInfo } from "@playwright/test";
import type { Fixture, Theme } from "./fixtures";

/** The colour scheme of the current Playwright project (`light` or `dark`). */
export function themeOf(testInfo: TestInfo): Theme {
  return testInfo.project.name === "dark" ? "dark" : "light";
}

/** The app follows `prefers-color-scheme` until the user picks one: check it did. */
export async function expectScheme(page: Page, theme: Theme): Promise<void> {
  const html = page.locator("html");
  if (theme === "dark") await expect(html).toHaveClass(/(^|\s)dark(\s|$)/);
  else await expect(html).not.toHaveClass(/(^|\s)dark(\s|$)/);
}

/**
 * Screens 3-4: add a local repository through the wizard's first step. `by: "list"`
 * picks it from the shared-folder list, `by: "path"` types its path and checks it.
 * Ends on the Configure step.
 */
export async function addLocalProject(page: Page, fx: Fixture, by: "list" | "path"): Promise<void> {
  await page.goto("/projects/new");
  if (by === "list") {
    await page.getByRole("radio", { name: new RegExp(`${fx.slug}\\b`) }).check();
  } else {
    await page.getByLabel("Or enter a path").fill(fx.path);
    await page.getByRole("button", { name: "Check" }).click();
  }
  await expect(page.getByText("Ready to add")).toBeVisible();
  await page.getByRole("button", { name: "Add project" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${fx.slug}/init\\?step=configure`));
}

/** Configure (keep the defaults) and Initialize with Claude Code, ending on the first-scan step. */
export async function configureAndInitialize(page: Page, fx: Fixture): Promise<void> {
  await page.getByRole("button", { name: "Save and continue" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${fx.slug}/init\\?step=initialize`));

  await page.getByRole("checkbox", { name: /Claude Code/ }).check();
  await expect(page.getByTestId("init-preview")).toBeVisible();
  await page.getByRole("button", { name: "Initialize", exact: true }).click();
  await expect(page.getByTestId("init-done")).toBeVisible();
  await page.getByRole("button", { name: "Continue to first scan" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${fx.slug}/init\\?step=scan`));
}

/** A link in the sidebar (project pages also carry same-named buttons in their bodies). */
export function sidebarLink(page: Page, name: string): Locator {
  return page.getByRole("navigation", { name: "Main" }).getByRole("link", { name, exact: true });
}

/** In the Explorer, expand src/ -> the fixture's file -> its main function and select it. */
export async function openMainSymbol(page: Page, fx: Fixture): Promise<void> {
  // The graph canvas names `src` too; the tree pane is the one to click. A click
  // toggles, so expand only what is not open yet.
  const tree = page.getByTestId("tree");
  const file = tree.getByText(fx.fileName, { exact: true });
  if (!(await file.isVisible())) await tree.getByText("src", { exact: true }).click();
  await file.click();
  await tree.getByText(fx.mainSymbol, { exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`node=${encodeURIComponent(fx.qualifiedMain)}`));
  await expect(page.getByText(fx.qualifiedMain, { exact: true }).first()).toBeVisible();
}

/** Run the first (structure-only) scan and leave the wizard for the project. */
export async function firstScan(page: Page, fx: Fixture): Promise<void> {
  await page.getByRole("button", { name: "Start first scan" }).click();
  await expect(page.getByTestId("scan-progress")).toBeVisible();
  await expect(page.getByText("First scan complete")).toBeVisible({ timeout: 30_000 });
  // The fake scanner records no commits, so the cost card has nothing to describe:
  // it offers "Open project" where a real scan would offer "Later".
  await expect(page.getByText("Nothing to describe")).toBeVisible();
  await page.getByRole("button", { name: "Open project" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${fx.slug}$`));
}

/**
 * Start a quick rescan from the project's Rescan button: a menu for a project
 * admin (the default, and what local mode always shows), a plain button for a
 * contributor (`menu: false`).
 */
export async function quickRescan(page: Page, menu = true): Promise<void> {
  await page.getByRole("button", { name: "Rescan", exact: true }).first().click();
  if (menu) await page.getByRole("menuitem", { name: "Quick rescan" }).click();
}
