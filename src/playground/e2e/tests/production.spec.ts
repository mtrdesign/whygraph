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

/** The administrator's password form, behind its disclosure on the sign-in page. */
async function signIn(page: Page, email: string): Promise<void> {
  await page.getByRole("button", { name: "Administrator sign-in" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

/** Sign in with GitHub through the fake: the button, then "Continue as <login>". */
async function githubSignIn(page: Page, login: string): Promise<void> {
  await page.goto("/signin");
  await page.getByRole("button", { name: "Sign in with GitHub" }).click();
  await page.getByRole("link", { name: `Continue as ${login}` }).click();
}

test("bootstrap, organizations on their own hosts, sign-in hand-off and reader access", async ({ page, browser }) => {
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

  // Ben (a second browser context, so his cookies are his own) signs in with
  // GitHub and creates bravo (the plan says beta, which is a reserved slug).
  const benContext = await browser.newContext({ baseURL: env.prodUrl });
  const ben = await benContext.newPage();
  await githubSignIn(ben, "ben");
  await expect(ben).toHaveURL(/\/orgs\/new$/);
  await createOrg(ben, "Bravo", "bravo");

  // Ben is signed in but not a member of acme.
  await ben.goto(`${orgUrl("acme")}/`);
  await expect(ben.getByRole("heading", { name: "No access to this organization" })).toBeVisible();

  // Ada adds ben on acme's Members page; Ben reloads and sees Projects.
  await page.goto(`${orgUrl("acme")}/members`);
  await expect(page.getByRole("heading", { name: "Members" })).toBeVisible();
  await page.getByLabel("GitHub username").fill("ben");
  await page.getByRole("button", { name: "Add member" }).click();
  await expect(page.getByTestId("member-list")).toContainText("@ben");
  await ben.reload();
  await expect(ben.getByRole("heading", { name: "Projects" })).toBeVisible();

  // Promoted to admin, Ben gets the Members page's controls; then Ada removes him.
  await page.getByLabel("Role for Ben").selectOption("admin");
  await expect(page.getByLabel("Role for Ben")).toHaveValue("admin");
  await ben.goto(`${orgUrl("acme")}/members`);
  await expect(ben.getByLabel("GitHub username")).toBeVisible();
  await page.getByTestId("member-list").getByRole("listitem").filter({ hasText: "@ben" })
    .getByRole("button", { name: "Remove" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Remove", exact: true }).click();
  await expect(page.getByTestId("member-list")).not.toContainText("@ben");
  await ben.reload();
  await expect(ben.getByRole("heading", { name: "No access to this organization" })).toBeVisible();
  await signOut(ben, "button");
  await benContext.close();

  // A GitHub account without two-factor authentication is refused.
  const nofaContext = await browser.newContext({ baseURL: env.prodUrl });
  const nofa = await nofaContext.newPage();
  await githubSignIn(nofa, "nofa");
  await expect(nofa.getByTestId("github-callback-error")).toContainText("two-factor authentication");
  await nofaContext.close();

  await signOut(page);

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

/** Wait until a GitHub sign-in (or a password one) has landed back on WhyGraph and rendered. */
async function signedIn(page: Page): Promise<void> {
  await expect(page).toHaveURL(
    (url) =>
      url.hostname.endsWith(base.hostname) && !/^\/(auth\/|signin)/.test(url.pathname),
  );
  await page.waitForLoadState();
  await expect(page.getByRole("heading").first()).toBeVisible();
}

/** The scan runs of a project, read in the page (same origin, its own cookies). */
async function runs(page: Page, slug: string): Promise<{ kind: string; trigger: string; status: string }[]> {
  return page.evaluate(async (s) => {
    const r = await fetch(`/api/projects/${s}/scans`, { headers: { "X-WhyGraph-Client": "1" } });
    if (!r.ok) throw new Error(`scans answered ${r.status}`);
    return ((await r.json()) as { runs: { kind: string; trigger: string; status: string }[] }).runs;
  }, slug);
}

test("projects from GitHub: connect, import, scan, members, a push, deleting the org", async ({ page, browser }) => {
  // Ben (GitHub) creates rocket and imports the fake's fixture repo ben/demo
  // through the GitHub App (the fake installs it on his account at start).
  const benContext = await browser.newContext({ baseURL: env.prodUrl });
  const ben = await benContext.newPage();
  await githubSignIn(ben, "ben");
  await signedIn(ben);
  await ben.goto("/orgs/new");
  await createOrg(ben, "Rocket", "rocket");
  await ben.getByRole("link", { name: "Import from GitHub" }).click();
  await expect(ben.getByRole("heading", { name: "Import from GitHub" })).toBeVisible();

  // No user authorization yet in this session: Connect GitHub, authorize as
  // ben on the fake, and the callback page sends him back to the import page.
  await ben.getByTestId("github-connect").getByRole("button", { name: "Connect GitHub" }).click();
  await ben.getByRole("link", { name: "Continue as ben" }).click();
  await expect(ben).toHaveURL(new RegExp(`^${orgUrl("rocket")}/projects/new`));
  await expect(ben.getByRole("radiogroup", { name: "GitHub accounts" }).getByRole("radio", { name: "ben" })).toBeChecked();
  await expect(ben.getByTestId("repo-ben/notes")).toBeVisible();
  await ben.getByTestId("repo-ben/demo").getByRole("button", { name: "Import ben/demo" }).click();

  // The import cloned it: configure, then the first scan (the stub scanner,
  // which fails on an unreadable token file) completes.
  await expect(ben).toHaveURL(new RegExp(`/p/demo/init\\?step=configure`));
  await ben.getByRole("button", { name: "Save and continue" }).click();
  await expect(ben).toHaveURL(new RegExp(`/p/demo/init\\?step=scan`));
  await ben.getByRole("button", { name: "Start first scan" }).click();
  await expect(ben.getByText("First scan complete")).toBeVisible({ timeout: 30_000 });
  await ben.getByRole("button", { name: "Open project" }).click();
  await expect(ben).toHaveURL(new RegExp(`/p/demo$`));
  expect((await runs(ben, "demo")).map((r) => r.status)).toEqual(["ok"]);

  // Ada, the instance administrator, signs in with a password: she cannot
  // import from GitHub, not even into her own organization.
  // Signed out, acme's import page sends her to sign in and back (no second
  // navigation that could race the sign-in's own).
  await page.goto(`${orgUrl("acme")}/projects/new`);
  await expect(page).toHaveURL(new RegExp(`^${base.origin}/signin\\?next=`));
  const refused = page.waitForResponse((r) => r.url().endsWith("/api/github/installations"));
  await signIn(page, "ada@example.com");
  await expect(page).toHaveURL(`${orgUrl("acme")}/projects/new`);
  const answer = await refused;
  expect(answer.status()).toBe(403);
  expect(((await answer.json()) as { code?: string }).code).toBe("github_required");
  await expect(page.getByTestId("github-error")).toContainText("needs an account that signs in with GitHub");

  // cy signs in once, Ben adds him as a member (M2d-1's flow): cy sees the
  // project, without the owners' and admins' New project.
  const cyContext = await browser.newContext({ baseURL: env.prodUrl });
  const cy = await cyContext.newPage();
  await githubSignIn(cy, "cy");
  await signedIn(cy);
  await ben.goto(`${orgUrl("rocket")}/members`);
  await ben.getByLabel("GitHub username").fill("cy");
  await ben.getByRole("button", { name: "Add member" }).click();
  await expect(ben.getByTestId("member-list")).toContainText("@cy");
  await expect(ben.getByLabel("Role for Cy")).toHaveValue("member");
  await cy.goto(`${orgUrl("rocket")}/`);
  await expect(cy.getByTestId("project-demo")).toBeVisible();
  await expect(cy.getByRole("link", { name: "New project" })).toHaveCount(0);
  await cyContext.close();

  // A push on the fake: its webhook delivery queues a sync + scan of the project.
  const pushed = await fetch(`${env.githubUrl}/_fake/push`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo: "ben/demo", message: "Pushed in e2e" }),
  });
  expect(pushed.status).toBe(200);
  expect(((await pushed.json()) as { webhook_status: number }).webhook_status).toBe(202);
  await expect
    .poll(async () => (await runs(ben, "demo")).find((r) => r.trigger === "push")?.status, { timeout: 30_000 })
    .toBe("ok");

  // Ben, the owner, deletes rocket; its slug can never be created again.
  await ben.goto(`${orgUrl("rocket")}/settings`);
  await ben.getByTestId("org-danger-zone").getByRole("button", { name: "Delete organization" }).click();
  const dialog = ben.getByTestId("delete-org-dialog");
  await dialog.getByLabel("Type rocket to confirm").fill("rocket");
  await dialog.getByRole("button", { name: "Delete organization" }).click();
  await expect(ben).toHaveURL(new RegExp(`^${base.origin}/orgs$`));
  await ben.goto("/orgs/new");
  await ben.getByLabel("Organization name").fill("Rocket");
  await ben.getByLabel("URL name").fill("rocket");
  await ben.getByRole("button", { name: "Create organization" }).click();
  await expect(ben.getByText("That URL name is already taken.")).toBeVisible();
  await benContext.close();
});
