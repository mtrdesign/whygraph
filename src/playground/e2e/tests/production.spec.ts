import fs from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { env } from "../env";
import { sidebarLink } from "../lib/ui";
import { base, createOrg, githubSignIn, importRepo, orgUrl, signedIn } from "../lib/production";

// Production mode (M2c): the bootstrap secret, organizations on their own hosts,
// the shared session cookie and the instance admin's read-only access. Runs
// against its own portal (`env.prodUrl`), independent of the local-mode projects.
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

/** The administrator's password form, behind its disclosure on the sign-in page. */
async function signIn(page: Page, email: string): Promise<void> {
  await page.getByRole("button", { name: "Administrator sign-in" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
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

  // The org switcher names the org; "All organizations" lists it on the base host
  // even though it is her only one (`?stay=1`), with a way back (NAV-2, NAV-7).
  await page.getByTestId("org-switcher").click();
  await expect(page.getByTestId("org-current")).toContainText("Acme");
  await page.getByRole("menuitem", { name: "All organizations" }).click();
  await expect(page).toHaveURL(new RegExp(`^${base.origin}/orgs\\?stay=1&from=acme$`));
  await expect(page.getByTestId("org-list")).toContainText("Acme");
  await page.getByTestId("back-to-org").click();
  await expect(page).toHaveURL(new RegExp(`^${orgUrl("acme")}/`));
  await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();

  // Ben (a second browser context, so his cookies are his own) signs in with
  // GitHub and creates bravo (the plan says beta, which is a reserved slug).
  const benContext = await browser.newContext({ baseURL: env.prodUrl });
  const ben = await benContext.newPage();
  await githubSignIn(ben, "ben");
  // No organization yet: the picker says so (no redirect) and offers to create one.
  await expect(ben.getByRole("heading", { name: "You're not in an organization yet" })).toBeVisible();
  await expect(ben.getByTestId("join-hint")).toContainText("@ben");
  await ben.getByRole("link", { name: "Create organization" }).click();
  await expect(ben).toHaveURL(/\/orgs\/new$/);
  await createOrg(ben, "Bravo", "bravo");

  // Ben is signed in but not a member of acme.
  await ben.goto(`${orgUrl("acme")}/`);
  await expect(ben.getByRole("heading", { name: "No access to this organization" })).toBeVisible();

  // Ada adds ben on acme's Members page; Ben reloads and sees Projects.
  await page.goto(`${orgUrl("acme")}/members`);
  await expect(page.getByRole("heading", { name: "Members", exact: true, level: 1 })).toBeVisible();
  await expect(page.getByTestId("members-heading")).toContainText("Members (");
  await page.getByLabel("GitHub username").fill("ben");
  await page.getByRole("button", { name: "Invite", exact: true }).click();
  await expect(page.getByTestId("member-list")).toContainText("@ben");
  await ben.reload();
  await expect(ben.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();

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

  // The account menu shows who and in which role, the version inside it, and no
  // "Switch organization" (the org switcher took that over).
  await expect(page.getByTestId("account-who")).toContainText("ada@example.com · Owner");
  await page.getByRole("button", { name: "Account menu" }).click();
  await expect(page.getByText(/^WhyGraph \d/)).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Switch organization" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  await signOut(page);

  // Signed out, acme's host sends the browser to the base host's sign-in.
  await page.goto(`${orgUrl("acme")}/`);
  await expect(page).toHaveURL(new RegExp(`^${base.origin}/signin\\?next=`));
  expect(new URL(page.url()).searchParams.get("next")).toContain(orgUrl("acme"));
  await expect(page.getByText(`Sign in to continue to ${new URL(orgUrl("acme")).host}.`)).toBeVisible();

  // Signing in as Ada hands her back to acme.
  await signIn(page, "ada@example.com");
  await expect(page).toHaveURL(new RegExp(`^${orgUrl("acme")}/`));
  await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();

  // bravo's host needs no new sign-in (the cookie covers the subdomains); as an
  // instance admin who is not a member, Ada reads it behind the banner.
  await page.goto(`${orgUrl("bravo")}/`);
  await expect(page.getByRole("heading", { name: "Projects", exact: true })).toBeVisible();
  await expect(page.getByTestId("reader-banner")).toBeVisible();
  await expect(page).not.toHaveURL(/\/signin/);
});

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
  // Before the import, a new org's empty page is the first-run checklist.
  await expect(ben.getByTestId("first-run-checklist")).toContainText("No projects yet");
  await expect(ben.getByTestId("first-run-project")).toContainText("Import a repository");
  await expect(ben.getByTestId("first-run-invite")).toContainText("Invite your team");
  await importRepo(ben, "rocket", "ben/demo");
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
  await expect(page.getByTestId("github-required")).toContainText("Importing needs a GitHub sign-in");

  // cy signs in once, Ben adds him as a member (M2d-1's flow): cy sees the
  // project, without the owners' and admins' New project.
  const cyContext = await browser.newContext({ baseURL: env.prodUrl });
  const cy = await cyContext.newPage();
  await githubSignIn(cy, "cy");
  await signedIn(cy);
  await ben.goto(`${orgUrl("rocket")}/members`);
  await ben.getByLabel("GitHub username").fill("cy");
  await ben.getByRole("button", { name: "Invite", exact: true }).click();
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
  // Only bravo is left, so the picker sends him to its host. Let that landing
  // happen before asking for the creation page: a navigation of our own while
  // the app is redirecting is aborted (`net::ERR_ABORTED`).
  await expect(ben).toHaveURL(new RegExp(`^${orgUrl("bravo")}/`));
  await ben.goto("/orgs/new");
  await ben.getByLabel("Organization name").fill("Rocket");
  await ben.getByLabel("URL name").fill("rocket");
  // The live check already says so before submit; the server still refuses it.
  await expect(ben.getByTestId("slug-status")).toContainText("rocket." + base.host + " is already taken");
  await ben.getByRole("button", { name: "Create organization" }).click();
  await expect(ben.getByText("That URL name is already taken.")).toBeVisible();
  await benContext.close();
});

test("project access: Restricted projects, an invited user's grants, roles and an ownership transfer", async ({
  browser,
}) => {
  // Two sign-ins, two imports with first scans and three more browsers.
  test.setTimeout(300_000);
  const context = (login: string) => browser.newContext({ baseURL: env.prodUrl }).then(async (c) => {
    const page = await c.newPage();
    await githubSignIn(page, login);
    await signedIn(page);
    return { c, page };
  });
  const rescan = (page: Page) => page.getByRole("button", { name: "Rescan", exact: true });

  // Ben creates comet, imports both fixture repos and restricts both.
  const { c: benContext, page: ben } = await context("ben");
  await ben.goto(`${base.origin}/orgs/new`);
  await createOrg(ben, "Comet", "comet");
  await importRepo(ben, "comet", "ben/demo");
  await importRepo(ben, "comet", "ben/notes");
  for (const slug of ["demo", "notes"]) {
    await ben.goto(`${orgUrl("comet")}/p/${slug}/settings`);
    const access = ben.getByTestId("project-access");
    await access.getByRole("switch", { name: "Restricted" }).click();
    await expect(access.getByTestId("access-default")).toContainText("no access (Restricted)");
  }

  // Ben invites dee, who has never signed in (the fake knows her), as a member
  // with a Viewer grant on demo only: no message, a link to share.
  await ben.goto(`${orgUrl("comet")}/members`);
  await ben.getByLabel("GitHub username").fill("dee");
  await ben.getByLabel("Access to demo").selectOption("viewer");
  await ben.getByRole("button", { name: "Invite", exact: true }).click();
  await expect(ben.getByTestId("invite-pending")).toBeVisible();
  await expect(ben.getByTestId("invitations")).toContainText("@dee");
  await expect(ben.getByTestId("invitations")).toContainText("demo: Viewer");

  // Dee signs in and is a member at once: she sees demo, not notes; she may
  // look but not rescan, and has no Chats section.
  const { c: deeContext, page: dee } = await context("dee");
  await dee.goto(`${orgUrl("comet")}/`);
  await expect(dee.getByTestId("project-demo")).toBeVisible();
  await expect(dee.getByTestId("project-notes")).toHaveCount(0);
  await dee.goto(`${orgUrl("comet")}/p/demo`);
  await expect(sidebarLink(dee, "Scans")).toBeVisible();
  await expect(dee.getByTestId("chats-section")).toHaveCount(0);
  await expect(rescan(dee)).toHaveCount(0);
  // The restricted project she holds no grant on answers as if it did not exist.
  const notes = await dee.request.get(`${orgUrl("comet")}/api/projects/notes`, { headers: { "X-WhyGraph-Client": "1" } });
  expect(notes.status()).toBe(404);

  // Ben raises her to Contributor on demo: a plain Rescan, no Full rescan.
  await ben.goto(`${orgUrl("comet")}/p/demo/settings`);
  const person = ben.getByTestId("project-access").getByTestId("access-people").getByRole("listitem")
    .filter({ hasText: "@dee" });
  await person.getByRole("combobox").selectOption("contributor");
  await expect(person.getByRole("combobox")).toHaveValue("contributor");
  await dee.reload();
  await expect(rescan(dee).first()).toBeVisible();
  await expect(dee.getByTestId("chats-section").first()).toBeVisible();
  await rescan(dee).first().click();
  await expect(dee.getByRole("menuitem")).toHaveCount(0);
  await expect(dee.getByText("Full rescan")).toHaveCount(0);
  await deeContext.close();

  // Contrast: Ben, a project admin, gets the menu with both choices.
  await ben.goto(`${orgUrl("comet")}/p/demo`);
  await rescan(ben).first().click();
  await expect(ben.getByRole("menuitem", { name: "Quick rescan" })).toBeVisible();
  await expect(ben.getByRole("menuitem", { name: "Full rescan" })).toBeVisible();
  await ben.keyboard.press("Escape");

  // Cy signs in, Ben adds him, then hands him the organization (typed slug).
  const { c: cyContext, page: cy } = await context("cy");
  await ben.goto(`${orgUrl("comet")}/members`);
  await ben.getByLabel("GitHub username").fill("cy");
  await ben.getByRole("button", { name: "Invite", exact: true }).click();
  await expect(ben.getByTestId("member-list")).toContainText("@cy");
  await ben.goto(`${orgUrl("comet")}/settings`);
  const ownership = ben.getByTestId("org-ownership");
  await ownership.getByLabel("New owner").selectOption({ label: "Cy" });
  await ownership.getByRole("button", { name: "Transfer ownership" }).click();
  const dialog = ben.getByTestId("transfer-dialog");
  await dialog.getByLabel("Type comet to confirm").fill("comet");
  await dialog.getByRole("button", { name: "Transfer ownership" }).click();
  await expect(ben.getByTestId("org-ownership")).toHaveCount(0);

  // Cy, now the owner, finds all of it on the audit page. The writer batches
  // for up to a second, so poll with reloads.
  await cy.goto(`${orgUrl("comet")}/audit`);
  const table = cy.getByTestId("audit-table");
  await expect
    .poll(
      async () => {
        await cy.reload();
        await expect(table).toBeVisible();
        const text = await table.innerText();
        return ["Invitation created", "Restricted setting changed", "Project access changed", "Ownership transferred"]
          .filter((e) => !text.includes(e));
      },
      { timeout: 30_000 },
    )
    .toEqual([]);
  await expect(table).toContainText("@ben");
  await cyContext.close();
  await benContext.close();
});
