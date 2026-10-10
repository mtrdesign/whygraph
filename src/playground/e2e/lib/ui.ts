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
 * Add a local repository through the wizard's first step. `by: "list"` picks it
 * from the shared-folder list, `by: "path"` types its path and checks it. Ends on
 * the Set up step.
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
  await expect(page).toHaveURL(new RegExp(`/p/${fx.slug}/init\\?step=setup`));
}

/**
 * Set the project up with Claude Code (the wizard's Set up step, `POST /init`),
 * which queues the first scan: ends on Configure, following that run.
 */
export async function setUpProject(page: Page, fx: Fixture): Promise<void> {
  await page.getByRole("checkbox", { name: /Claude Code/ }).check();
  await expect(page.getByTestId("init-preview")).toBeVisible();
  await page.getByRole("button", { name: "Set up project", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${fx.slug}/init\\?step=configure&run=\\d+`));
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

/** Wait for the first (structure-only) scan on Configure and leave the wizard for the project. */
export async function firstScan(page: Page, fx: Fixture): Promise<void> {
  // Set up queued the first scan; Configure follows that run.
  await expect(page.getByText("First scan complete")).toBeVisible({ timeout: 30_000 });
  // The fake scanner records no commits: the estimate says so (BUG-14).
  await expect(page.getByText("No commits yet. Push some history, then rescan.")).toBeVisible();
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
  if (menu) {
    // The menu opens at the button, but the page can scroll under it while the project's
    // charts and cards settle, which leaves the popup outside the viewport and unclickable
    // (a flake seen twice). A keyboard activation does not care where the popup is.
    const item = page.getByRole("menuitem", { name: "Quick rescan" });
    await expect(item).toBeAttached();
    await item.focus();
    await page.keyboard.press("Enter");
  }
}

/**
 * Press Tab until `target` has focus (at most `max` presses), proving a keyboard
 * user reaches it. Starts from wherever focus is.
 */
export async function tabUntil(page: Page, target: Locator, max = 80): Promise<void> {
  for (let i = 0; i < max; i++) {
    if (await target.evaluate((el) => el === document.activeElement)) return;
    await page.keyboard.press("Tab");
  }
  await expect(target).toBeFocused();
}

/** The skip link is the first stop of a fresh page and moves focus to `<main>`. */
export async function expectSkipLink(page: Page): Promise<void> {
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeFocused();
}
