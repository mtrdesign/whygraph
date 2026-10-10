import { expect, type Page } from "@playwright/test";
import { env } from "../env";

// Helpers shared by the specs that drive the production-mode portal
// (`production.spec.ts`, `linked.spec.ts`).
export const base = new URL(env.prodUrl);

/** The host of one organization on the production portal. */
export const orgUrl = (slug: string) => `${base.protocol}//${slug}.${base.host}`;

/**
 * Sign in with GitHub through the fake: the button, then "Continue as <login>".
 * Starts from `/signin` unless the page is already on the sign-in page (a
 * redirect from a protected page keeps its `next`).
 */
export async function githubSignIn(page: Page, login: string): Promise<void> {
  if (!/^\/signin/.test(new URL(page.url(), base.origin).pathname)) {
    await page.goto("/signin");
  }
  await page.getByRole("button", { name: "Sign in with GitHub" }).click();
  await page.getByRole("link", { name: `Continue as ${login}` }).click();
}

/**
 * Wait until a sign-in (GitHub or password) has landed back on WhyGraph and
 * rendered. The callback page navigates by itself once the exchange is done, and
 * a navigation of ours that races it is aborted - so nothing may `goto` before
 * this resolves.
 */
export async function signedIn(page: Page): Promise<void> {
  await expect(page).toHaveURL(
    (url) => url.hostname.endsWith(base.hostname) && !/^\/(auth\/|signin)/.test(url.pathname),
  );
  await page.waitForLoadState();
  await expect(page.getByRole("heading").first()).toBeVisible();
}

/** Fill the organization-creation form (the page is already on it) and wait for the new org's host. */
export async function createOrg(page: Page, name: string, slug: string): Promise<void> {
  await page.getByLabel("Organization name").fill(name);
  await page.getByLabel("URL name").fill(slug);
  await expect(page.getByTestId("slug-preview")).toHaveText(`${slug}.${base.host}`);
  await page.getByRole("button", { name: "Create organization" }).click();
  await expect(page).toHaveURL(new RegExp(`^${orgUrl(slug)}/`));
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
}

/**
 * Leave the wizard's first-scan step. The production portal's fake scanner runs
 * `--real-git`, so the project has commits waiting for a description and the cost
 * card offers *Later*; with none it offers *Open project* instead.
 */
export async function openAfterFirstScan(page: Page): Promise<void> {
  const later = page.getByRole("button", { name: "Later", exact: true });
  const open = page.getByRole("button", { name: "Open project", exact: true });
  await expect(later.or(open)).toBeVisible();
  if (await later.isVisible()) await later.click();
  else await open.click();
}

/**
 * Import `owner/name` into `org` through the GitHub App (connecting GitHub first
 * when this session has not), run the first scan and end on the project's page
 * (`/p/<name>`). The page must belong to the repo owner's signed-in session.
 */
export async function importRepo(page: Page, org: string, repo: string): Promise<void> {
  const [owner, slug] = repo.split("/");
  await page.goto(`${orgUrl(org)}/projects/new`);
  const connect = page.getByTestId("github-connect").getByRole("button", { name: "Connect GitHub" });
  const row = page.getByTestId(`repo-${repo}`);
  await expect(connect.or(row)).toBeVisible();
  if (await connect.isVisible()) {
    await connect.click();
    await page.getByRole("link", { name: `Continue as ${owner}` }).click();
    await expect(page).toHaveURL(new RegExp(`^${orgUrl(org)}/projects/new`));
  }
  await page.getByRole("checkbox", { name: `Select ${repo}` }).click();
  await page.getByRole("button", { name: "Import 1 repository" }).click();

  // The request only queues the run: it clones, then scans, and Configure follows it.
  await expect(page).toHaveURL(new RegExp(`/p/${slug}/init\\?step=configure&run=\\d+`));
  await expect(page.getByText("First scan complete")).toBeVisible({ timeout: 30_000 });
  await openAfterFirstScan(page);
  await expect(page).toHaveURL(new RegExp(`/p/${slug}$`));
}
