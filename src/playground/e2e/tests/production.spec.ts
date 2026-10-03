import fs from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { env } from "../env";

// Production mode (M2c): the bootstrap secret, organizations on their own hosts,
// the shared session cookie and the instance admin's read-only access. Runs
// against its own portal (`env.prodUrl`), independent of the local-mode projects.
const base = new URL(env.prodUrl);
const orgUrl = (slug: string) => `${base.protocol}//${slug}.${base.host}`;
const PASSWORD = "correct horse battery staple";

/** The one-time secret the portal prints in its log (plan 4.3). */
function bootstrapSecret(): string {
  const m = /Bootstrap secret: ([A-Za-z0-9_-]{24})/.exec(fs.readFileSync(env.prodLog, "utf8"));
  if (!m) throw new Error(`no bootstrap secret in ${env.prodLog}`);
  return m[1];
}

/** Sign out through the sidebar's account menu, or the no-access page's own button. */
async function signOut(page: Page, via: "menu" | "button" = "menu"): Promise<void> {
  if (via === "menu") {
    await page.getByRole("button", { name: "Account menu" }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
  } else {
    await page.getByRole("button", { name: "Sign out" }).click();
  }
  await expect(page).toHaveURL(/\/signin/);
}

async function createOrg(page: Page, name: string, slug: string): Promise<void> {
  await page.getByLabel("Organization name").fill(name);
  await page.getByLabel("URL name").fill(slug);
  await expect(page.getByTestId("slug-preview")).toHaveText(`${slug}.${base.host}`);
  await page.getByRole("button", { name: "Create organization" }).click();
  await expect(page).toHaveURL(new RegExp(`^${orgUrl(slug)}/`));
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
}

async function signIn(page: Page, email: string): Promise<void> {
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("bootstrap, organizations on their own hosts, sign-in hand-off and reader access", async ({ page }) => {
  // Bootstrap: the first visitor claims the instance with the logged secret.
  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);
  await page.getByLabel("Bootstrap secret").fill(bootstrapSecret());
  await page.getByLabel("Your name").fill("Ada Lovelace");
  await page.getByLabel("Email").fill("ada@example.com");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create administrator" }).click();

  // Ada has no organization yet: she creates acme and lands on its host.
  await expect(page).toHaveURL(/\/orgs\/new$/);
  await createOrg(page, "Acme", "acme");
  await expect(page.getByTestId("reader-banner")).toHaveCount(0);
  await signOut(page);

  // Ben registers and creates bravo (the plan says beta, which is a reserved slug).
  await page.goto("/register");
  await page.getByLabel("Your name").fill("Ben Bitdiddle");
  await page.getByLabel("Email").fill("ben@example.com");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/orgs\/new$/);
  await createOrg(page, "Bravo", "bravo");

  // Ben is signed in but not a member of acme.
  await page.goto(`${orgUrl("acme")}/`);
  await expect(page.getByRole("heading", { name: "No access to this organization" })).toBeVisible();
  await signOut(page, "button");

  // Signed out, acme's host sends the browser to the base host's sign-in.
  await page.goto(`${orgUrl("acme")}/`);
  await expect(page).toHaveURL(new RegExp(`^${base.origin}/signin\\?next=`));
  expect(new URL(page.url()).searchParams.get("next")).toContain(orgUrl("acme"));

  // Signing in as Ada hands her back to acme.
  await signIn(page, "ada@example.com");
  await expect(page).toHaveURL(new RegExp(`^${orgUrl("acme")}/`));
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();

  // bravo's host needs no new sign-in (the cookie covers the subdomains); as an
  // instance admin who is not a member, Ada reads it behind the banner.
  await page.goto(`${orgUrl("bravo")}/`);
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
  await expect(page.getByTestId("reader-banner")).toBeVisible();
  await expect(page).not.toHaveURL(/\/signin/);
});
