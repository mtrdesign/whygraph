import fs from "node:fs";
import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";
import { env } from "../env";
import { base, createOrg, githubSignIn, importRepo, openAfterFirstScan, orgUrl, signedIn } from "../lib/production";
import { attempt, gap, keepShell, shoot, variants, watch, type ShotMeta } from "./lib/shoot";
import { api, chat, configureAuditLlm, prodControl } from "./lib/seed";

// The production-mode half of the screenshot audit: bootstrap, sign-in, orgs,
// the GitHub App import, members and invitations, project access, Usage & cost
// with seeded usage and budgets, the audit log, the instance admin, a reader and
// a Viewer. One long test with one browser context per person; each screen is an
// `attempt` (a failure becomes a gap, not the end of the run).

const M = "production" as const;
const PASSWORD = "correct horse battery staple";
const ROCKET = orgUrl("rocket");
const ctl = prodControl();
const at = (area: string, name: string, state = "default") => ({ mode: M, area, name, state });
const snap = (page: Page, area: string, name: string, state: string, desc: string, opts?: Parameters<typeof shoot>[2]) =>
  shoot(page, { mode: M, area, name, state, desc } satisfies ShotMeta, opts);

function bootstrapSecret(): string {
  const m = /Bootstrap secret: ([A-Za-z0-9_-]{24})/.exec(fs.readFileSync(env.prodLog, "utf8"));
  if (!m) throw new Error(`no bootstrap secret in ${env.prodLog}`);
  return m[1];
}

async function person(browser: Browser, login: string | null): Promise<{ c: BrowserContext; page: Page }> {
  const c = await browser.newContext({ baseURL: env.prodUrl });
  const page = await c.newPage();
  watch(page);
  if (login) {
    await githubSignIn(page, login);
    await signedIn(page);
  }
  return { c, page };
}

interface MemberRow {
  uid: string;
  github_login: string | null;
}

async function settle(page: Page, ms = 1200): Promise<void> {
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => undefined);
  await page.waitForTimeout(ms);
}

test("production mode: every screen", async ({ page: ada, browser }) => {
  test.setTimeout(90 * 60_000);
  watch(ada);

  // ---- bootstrap (Ada, the instance administrator) --------------------------
  await attempt(ada, at("onboarding", "bootstrap", "empty"), async () => {
    await ada.goto("/");
    await expect(ada).toHaveURL(/\/setup$/);
    await settle(ada, 600);
    await snap(ada, "onboarding", "bootstrap", "empty", "Bootstrap / claim page on a fresh production portal (/ redirects to /setup)");
    await ada.getByLabel("Bootstrap secret").fill("not-the-secret-000000000");
    await ada.getByLabel("Your name").fill("Ada Lovelace");
    await ada.getByLabel("Email").fill("ada@example.com");
    await ada.getByLabel("Password").fill(PASSWORD);
    await ada.getByRole("button", { name: "Create administrator" }).click();
    await ada.waitForTimeout(1500);
    await snap(ada, "onboarding", "bootstrap", "wrong-secret", "Bootstrap submitted with a wrong secret");
  });
  if (/\/setup/.test(ada.url()) || !(await ada.getByLabel("Organization name").isVisible().catch(() => false))) {
    await ada.goto("/setup");
    await ada.getByLabel("Bootstrap secret").fill(bootstrapSecret());
    await ada.getByLabel("Your name").fill("Ada Lovelace");
    await ada.getByLabel("Email").fill("ada@example.com");
    await ada.getByLabel("Password").fill(PASSWORD);
    await ada.getByRole("button", { name: "Create administrator" }).click();
  }
  await expect(ada).toHaveURL(/\/orgs\/new$/);
  await attempt(ada, at("orgs", "create-org"), async () => {
    await settle(ada, 600);
    await snap(ada, "orgs", "create-org", "empty", "Create organization (/orgs/new): the claimed admin has no organization yet");
    await ada.getByLabel("Organization name").fill("Acme Corp");
    await ada.getByLabel("URL name").fill("acme");
    await ada.waitForTimeout(500);
    await snap(ada, "orgs", "create-org", "filled", "Create organization with name and URL name typed: the host preview");
    await ada.getByLabel("URL name").fill("beta");
    await ada.waitForTimeout(500);
    await snap(ada, "orgs", "create-org", "reserved-slug", "Create organization with a reserved URL name (beta)");
  });
  await ada.goto(`${base.origin}/orgs/new`);
  await createOrg(ada, "Acme Corp", "acme");
  await attempt(ada, at("projects", "projects", "empty-password-owner"), async () => {
    await settle(ada);
    await snap(ada, "projects", "projects", "empty-password-owner", "acme's empty Projects page, owner signed in with a password");
    await ada.goto(`${orgUrl("acme")}/projects/new`);
    await expect(ada.getByTestId("github-error")).toBeVisible();
    await settle(ada, 500);
    await snap(ada, "import", "github-import", "github-required", "Import page for a password account: 'needs an account that signs in with GitHub'");
  });

  // ---- sign-in (signed out) ---------------------------------------------------
  const anon = await person(browser, null);
  await attempt(anon.page, at("auth", "signin"), async () => {
    await anon.page.goto("/signin");
    await settle(anon.page, 600);
    await snap(anon.page, "auth", "signin", "default", "Sign-in page on the base host (/signin), signed out");
    await anon.page.getByRole("button", { name: "Administrator sign-in" }).click();
    await anon.page.waitForTimeout(400);
    await snap(anon.page, "auth", "signin", "admin-form-open", "Sign-in with the Administrator sign-in disclosure open");
    await anon.page.getByLabel("Email").fill("ada@example.com");
    await anon.page.getByLabel("Password").fill("wrong password here");
    await anon.page.getByRole("button", { name: "Sign in", exact: true }).click();
    await anon.page.waitForTimeout(1500);
    await snap(anon.page, "auth", "signin", "bad-credentials", "Administrator sign-in with a wrong password");
  });
  await attempt(anon.page, at("auth", "signin", "redirect-from-org"), async () => {
    await anon.page.goto(`${ROCKET.replace("rocket", "acme")}/`);
    await expect(anon.page).toHaveURL(/\/signin\?next=/);
    await settle(anon.page, 600);
    await snap(anon.page, "auth", "signin", "redirect-from-org", "An org host visited signed out: sent to /signin?next=<org url>");
  });
  await attempt(anon.page, at("auth", "reset", "no-token"), async () => {
    await anon.page.goto("/reset");
    await settle(anon.page, 600);
    await snap(anon.page, "auth", "reset", "no-token", "Password reset page without a token (/reset)");
  });
  await attempt(anon.page, at("auth", "github-callback", "2fa-refused"), async () => {
    await githubSignIn(anon.page, "nofa");
    await expect(anon.page.getByTestId("github-callback-error")).toBeVisible();
    await settle(anon.page, 500);
    await snap(anon.page, "auth", "github-callback", "2fa-refused", "GitHub sign-in callback for an account without 2FA (nofa): refused");
  });
  await attempt(anon.page, at("edge", "not-found", "unknown-org"), async () => {
    await anon.page.goto(`${orgUrl("nosuchorg")}/`);
    await settle(anon.page, 1000);
    await snap(anon.page, "edge", "not-found", "unknown-org-signed-out", "A host for an org that does not exist, signed out");
  });
  await anon.c.close();

  // ---- Ben: his orgs, the GitHub import --------------------------------------
  const { c: benC, page: ben } = await person(browser, "ben");
  await attempt(ben, at("orgs", "create-org", "github-user"), async () => {
    await expect(ben).toHaveURL(/\/orgs\/new$/);
    await settle(ben, 500);
    await snap(ben, "orgs", "create-org", "first-sign-in", "A GitHub user's first sign-in lands on Create organization (no orgs)");
  });
  await ben.goto(`${base.origin}/orgs/new`);
  await createOrg(ben, "Rocket Labs", "rocket");
  await attempt(ben, at("projects", "projects", "empty-owner"), async () => {
    await settle(ben);
    await snap(ben, "projects", "projects", "empty-github-owner", "rocket's empty Projects page, owner signed in with GitHub");
  });
  await attempt(ben, at("import", "github-import"), async () => {
    await ben.goto(`${ROCKET}/projects/new`);
    const connect = ben.getByTestId("github-connect");
    await expect(connect).toBeVisible();
    await settle(ben, 500);
    await snap(ben, "import", "github-import", "connect-github", "Import page before GitHub is connected for this session: the Connect GitHub card");
    await connect.getByRole("button", { name: "Connect GitHub" }).click();
    await ben.getByRole("link", { name: "Continue as ben" }).click();
    await expect(ben).toHaveURL(new RegExp(`^${ROCKET}/projects/new`));
    await expect(ben.getByTestId("repo-ben/demo")).toBeVisible();
    await settle(ben, 600);
    await snap(ben, "import", "github-import", "repo-list", "Import page after connecting: the installation's repositories (ben/demo, ben/notes)");
    await ben.getByTestId("repo-ben/demo").getByRole("button", { name: "Import ben/demo" }).click();
    await expect(ben).toHaveURL(/\/p\/demo\/init\?step=configure/);
    await settle(ben, 800);
    await snap(ben, "import", "wizard-configure", "default", "Imported ben/demo: wizard Configure step (production, models and keys only)");
    await ben.getByRole("button", { name: "Save and continue" }).click();
    await expect(ben).toHaveURL(/\/p\/demo\/init\?step=scan/);
    await settle(ben, 800);
    await snap(ben, "import", "wizard-scan", "ready", "Wizard first-scan step before Start first scan (production: no Initialize step)");
    await ben.getByRole("button", { name: "Start first scan" }).click();
    await ben.waitForTimeout(600);
    if (!(await ben.getByText("First scan complete").isVisible())) {
      await snap(ben, "import", "wizard-scan", "running", "Wizard first scan mid-run (production real-git scan, captured as fast as possible)", { widths: ["desktop"], themes: ["light"], viewportOnly: true });
    } else {
      gap(M, "import", "wizard-scan", "running", "the production first scan finished before the capture");
    }
    await expect(ben.getByText("First scan complete")).toBeVisible({ timeout: 60_000 });
    await settle(ben, 800);
    await snap(ben, "import", "wizard-scan", "done-cost-card", "First scan complete with the cost card: commits waiting for a description, Describe now / Later");
    await openAfterFirstScan(ben);
    await expect(ben).toHaveURL(/\/p\/demo$/);
    await settle(ben);
    await snap(ben, "project", "overview", "first-visit", "Project Overview of demo right after the import wizard (commits waiting for a description)");
  });
  await attempt(ben, at("import", "seed-notes"), async () => {
    await importRepo(ben, "rocket", "ben/notes");
  });
  await attempt(ben, at("import", "github-import", "all-imported"), async () => {
    await ben.goto(`${ROCKET}/projects/new`);
    await expect(ben.getByTestId("repo-ben/demo")).toBeVisible();
    await settle(ben, 600);
    await snap(ben, "import", "github-import", "all-imported", "Import page once both repositories are imported");
  });

  // A second org so the picker has something to pick from, and no-access.
  await ben.goto(`${base.origin}/orgs/new`);
  await createOrg(ben, "Bravo Team", "bravo");
  await attempt(ben, at("orgs", "org-picker"), async () => {
    await ben.goto(`${base.origin}/orgs`);
    await expect(ben.getByTestId("org-list").first()).toBeVisible();
    await settle(ben, 600);
    await snap(ben, "orgs", "org-picker", "two-orgs", "Org picker (/orgs) for a user in two organizations");
  });
  await attempt(ben, at("orgs", "no-org-access"), async () => {
    await ben.goto(`${orgUrl("acme")}/`);
    await expect(ben.getByRole("heading", { name: "No access to this organization" })).toBeVisible();
    await settle(ben, 500);
    await snap(ben, "orgs", "no-org-access", "default", "acme's host for a signed-in user who is not a member");
    await ben.goto(`${orgUrl("nosuchorg")}/`);
    await settle(ben, 1000);
    await snap(ben, "edge", "not-found", "unknown-org-signed-in", "A host for an org that does not exist, signed in");
  });
  await attempt(ben, at("orgs", "org-switcher"), async () => {
    await ben.goto(`${ROCKET}/`);
    await ben.getByRole("button", { name: "Account menu" }).click();
    await ben.waitForTimeout(400);
    await snap(ben, "orgs", "account-menu", "open", "The sidebar's account menu open (Switch organization, Account, Sign out)", { widths: ["desktop"], viewportOnly: true });
    await ben.keyboard.press("Escape");
  });

  // ---- members and invitations ----------------------------------------------
  await attempt(ben, at("members", "members", "owner-only"), async () => {
    await ben.goto(`${ROCKET}/members`);
    await expect(ben.getByRole("heading", { name: "Members" })).toBeVisible();
    await settle(ben);
    await snap(ben, "members", "members", "owner-only", "Members (/members) with only the owner, invite form empty");
  });
  // Cy signs in once (so he is added directly), dee never has (an invitation).
  const { c: cyC, page: cy } = await person(browser, "cy");
  await attempt(ben, at("members", "invite"), async () => {
    await ben.goto(`${ROCKET}/members`);
    await ben.getByLabel("GitHub username").fill("cy");
    await ben.getByLabel("Access to demo").selectOption("contributor");
    await settle(ben, 300);
    await snap(ben, "members", "invite-form", "filled", "Invite form filled: @cy, role member, Contributor on demo", { viewportOnly: true });
    await ben.getByRole("button", { name: "Invite", exact: true }).click();
    await expect(ben.getByTestId("member-list")).toContainText("@cy");
    await settle(ben, 500);
    await snap(ben, "members", "members", "added-directly", "After inviting @cy, who has signed in before: added at once");
    await ben.getByLabel("GitHub username").fill("dee");
    await ben.getByLabel("Access to demo").selectOption("viewer");
    await ben.getByRole("button", { name: "Invite", exact: true }).click();
    await expect(ben.getByTestId("invite-pending")).toBeVisible();
    await settle(ben, 500);
    await snap(ben, "members", "members", "invite-pending", "After inviting @dee, who never signed in: the pending-invitation notice and the invitations list");
    await ben.getByLabel("GitHub username").fill("nofa");
    const role = ben.getByLabel("Role", { exact: true });
    if (await role.isVisible().catch(() => false)) await role.selectOption("admin").catch(() => undefined);
    await ben.getByRole("button", { name: "Invite", exact: true }).click();
    await ben.waitForTimeout(1200);
    await ben.getByLabel("GitHub username").fill("no-such-user-xyz");
    await ben.getByRole("button", { name: "Invite", exact: true }).click();
    await ben.waitForTimeout(1500);
    await snap(ben, "members", "members", "unknown-login", "Inviting a GitHub login that does not exist: the add-member error; two invitations pending");
  });
  // Dee signs in: her invitation is redeemed.
  const { c: deeC, page: dee } = await person(browser, "dee");
  await attempt(ben, at("members", "members", "full"), async () => {
    await ben.goto(`${ROCKET}/members`);
    await settle(ben);
    await snap(ben, "members", "members", "full", "Members: owner, @cy (member, Contributor on demo), @dee (member, Viewer on demo), @nofa invitation pending");
    const row = ben.getByTestId("member-list").getByRole("listitem").filter({ hasText: "@dee" });
    await row.getByRole("button", { name: "Remove" }).click();
    await ben.waitForTimeout(400);
    await snap(ben, "members", "remove-member-dialog", "open", "Remove member confirmation for @dee", { viewportOnly: true });
    await ben.keyboard.press("Escape");
  });
  await variants(ben, `${ROCKET}/members`, { mode: M, area: "members", name: "members", what: "Members (/members)" });
  await attempt(dee, at("members", "joined"), async () => {
    await dee.goto(`${base.origin}/orgs`);
    await settle(dee, 800);
    await snap(dee, "orgs", "org-picker", "after-invitation", "Dee's org picker after her invitation was redeemed at sign-in");
  });

  // ---- project access: restricted notes, grants on demo ------------------
  await attempt(ben, at("access", "project-settings"), async () => {
    await ben.goto(`${ROCKET}/p/notes/settings`);
    const access = ben.getByTestId("project-access");
    await expect(access).toBeVisible();
    await access.getByRole("switch", { name: "Restricted" }).click();
    await expect(access.getByTestId("access-default")).toContainText("no access (Restricted)");
    await settle(ben, 600);
    await snap(ben, "access", "project-settings", "restricted", "Project settings of notes after switching on Restricted (Access section)");
    await ben.goto(`${ROCKET}/p/demo/settings`);
    await expect(ben.getByTestId("project-access")).toBeVisible();
    await settle(ben, 800);
    await snap(ben, "access", "project-settings", "grants", "Project settings of demo: Access section with @cy Contributor and @dee Viewer grants, Connected portals, Danger zone");
  });
  await variants(ben, `${ROCKET}/p/demo/settings`, { mode: M, area: "access", name: "project-settings", what: "Project settings (/p/demo/settings)" }, {
    match: keepShell([/^\/api\/projects\/demo$/, /^\/api\/projects$/, /^\/api\/org\/members$/]),
  });
  await attempt(ben, at("projects", "projects", "with-projects"), async () => {
    await ben.goto(`${ROCKET}/`);
    await expect(ben.getByTestId("project-demo")).toBeVisible();
    await settle(ben);
    await snap(ben, "projects", "projects", "with-projects", "rocket's Projects page: demo, and notes with the Restricted badge");
    await ben.goto(`${ROCKET}/p/notes`);
    await settle(ben);
    await snap(ben, "access", "overview", "restricted", "Overview of the Restricted project notes (owner's view)");
  });
  await variants(ben, `${ROCKET}/`, { mode: M, area: "projects", name: "projects", what: "rocket's Projects page" }, { states: ["loading", "error", "forbidden"] });

  // ---- chat, before and after the LLM is configured -----------------------
  await attempt(ben, at("chat", "chat", "no-llm-key"), async () => {
    await ben.goto(`${ROCKET}/p/demo/chat`);
    await ben.getByRole("button", { name: "New chat", exact: true }).first().click();
    await settle(ben, 1200);
    await snap(ben, "chat", "chat", "no-llm-key", "A new chat in an org with no LLM key");
  });
  await configureAuditLlm(ben.request, ROCKET);
  await attempt(ben, at("chat", "chat", "conversation-chart"), async () => {
    await chat(ben, `${ROCKET}/p/demo`, "How big are the symbols here? [[chart]]", "The chart above shows lines per symbol.");
    await settle(ben, 1500);
    await snap(ben, "chat", "chat", "conversation-chart", "Owner's chat on demo: tool cards, a bar chart, a Markdown answer (scripted LLM)");
  });
  await attempt(ben, at("seed", "chats"), async () => {
    await chat(ben, `${ROCKET}/p/notes`, "What is in notes?", "This repository is small");
    await chat(cy, `${ROCKET}/p/demo`, "Hello from cy", "This repository is small");
    await chat(cy, `${ROCKET}/p/demo`, "Chart it [[chart]]", "The chart above shows lines per symbol.");
  });
  await attempt(cy, at("chat", "chat", "member-sessions"), async () => {
    await cy.goto(`${ROCKET}/p/demo/chat`);
    await settle(cy, 1000);
    await snap(cy, "chat", "chat", "member-own-sessions", "Chat as @cy (member, Contributor): only his own sessions are listed");
  });

  // ---- the project pages (owner) ----------------------------------------
  await attempt(ben, at("project", "overview"), async () => {
    await ben.goto(`${ROCKET}/p/demo`);
    await settle(ben);
    await snap(ben, "project", "overview", "default", "Overview of demo (owner): stats, recent scans, commits waiting, Use with your agent");
  });
  await attempt(ben, at("explorer", "explorer"), async () => {
    await ben.goto(`${ROCKET}/p/demo/explorer`);
    await expect(ben.getByTestId("tree")).toBeVisible();
    await settle(ben, 1500);
    await snap(ben, "explorer", "explorer", "nothing-selected", "Explorer of demo, nothing selected");
    await ben.getByTestId("tree").getByText("src", { exact: true }).click();
    await ben.getByTestId("tree").getByText("demo.py", { exact: true }).click();
    await ben.getByTestId("tree").getByText("demo_main", { exact: true }).click();
    await expect(ben).toHaveURL(/node=/);
    await settle(ben, 1200);
    await snap(ben, "explorer", "node-detail", "relationships", "Explorer, demo_main selected: Relationships");
    for (const tab of ["Rationale", "Evidence", "History"]) {
      await ben.getByRole("tab", { name: tab }).click();
      await settle(ben, 1500);
      await snap(ben, "explorer", "node-detail", tab.toLowerCase(), `Explorer node detail, ${tab} tab (production project with real commits)`);
    }
  });

  // ---- scans: a push, a failure --------------------------------------------
  await attempt(ben, at("scans", "scan-run", "push"), async () => {
    const pushed = await fetch(`${env.githubUrl}/_fake/push`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo: "ben/demo", message: "Pushed during the audit" }),
    });
    expect(pushed.status).toBe(200);
    await ben.waitForTimeout(6000);
    ctl.fail(true);
    await ben.goto(`${ROCKET}/p/demo`);
    await ben.getByRole("button", { name: "Rescan", exact: true }).first().click();
    await ben.getByRole("menuitem", { name: "Quick rescan" }).click();
    await ben.waitForURL("**/p/demo/scans/*");
    await expect(ben.getByTestId("run-result")).toContainText("The scan failed", { timeout: 60_000 });
    await settle(ben, 800);
    await snap(ben, "scans", "scan-run", "failed", "A failed production scan run (control/fail) with its log");
    ctl.fail(false);
    await ben.goto(`${ROCKET}/p/demo/scans`);
    await expect(ben.getByTestId("scan-history")).toBeVisible();
    await settle(ben);
    await snap(ben, "scans", "scan-history", "import-push-failed", "Scan history of demo: the import's first scan, a push-triggered sync and a failed rescan");
    const first = ben.getByTestId("scan-history").getByRole("link").last();
    await first.click();
    await settle(ben, 1000);
    await snap(ben, "scans", "scan-run", "ok", "The first scan's run page (ok)");
  });
  ctl.fail(false);

  // ---- Viewer (dee) ------------------------------------------------------
  await attempt(dee, at("viewer", "projects"), async () => {
    await dee.goto(`${ROCKET}/`);
    await expect(dee.getByTestId("project-demo")).toBeVisible();
    await settle(dee);
    await snap(dee, "viewer", "projects", "member", "Projects as @dee (member, Viewer on demo): notes is Restricted and hidden");
    await dee.goto(`${ROCKET}/p/demo`);
    await settle(dee);
    await snap(dee, "viewer", "overview", "viewer", "Overview of demo as a Viewer: no Rescan, no Chat in the sidebar");
    await dee.goto(`${ROCKET}/p/demo/chat`);
    await settle(dee, 1500);
    await snap(dee, "viewer", "chat", "viewer-direct-url", "Chat opened by URL as a Viewer (no chat permission)");
    await dee.goto(`${ROCKET}/p/demo/explorer?node=demo.demo_main`);
    await settle(dee, 1500);
    await dee.getByRole("tab", { name: "Rationale" }).click().catch(() => undefined);
    await settle(dee, 800);
    await snap(dee, "viewer", "explorer-rationale", "viewer", "Explorer Rationale tab as a Viewer (no Generate)");
    await dee.goto(`${ROCKET}/p/demo/settings`);
    await settle(dee, 1200);
    await snap(dee, "viewer", "project-settings", "viewer", "Project settings as a Viewer");
    await dee.goto(`${ROCKET}/p/notes`);
    await settle(dee, 1500);
    await snap(dee, "viewer", "overview", "restricted-no-grant", "The Restricted project notes opened by URL without a grant (answers as not found)");
    await dee.goto(`${ROCKET}/usage`);
    await settle(dee, 1200);
    await snap(dee, "viewer", "my-usage", "no-usage", "/usage as a member with no LLM calls: sent to My usage (empty)");
  });

  // ---- Usage & cost, budgets ------------------------------------------
  await ben.waitForTimeout(2500);
  const members = await api<MemberRow[]>(ben.request, "GET", `${ROCKET}/api/org/members`);
  const uid = (login: string) => members.find((m) => m.github_login === login)?.uid ?? "";
  for (const tab of ["overview", "projects", "members", "models", "machines", "calls", "budgets", "prices"]) {
    await attempt(ben, at("usage", `usage-${tab}`), async () => {
      await ben.goto(`${ROCKET}/usage?tab=${tab}`);
      await settle(ben, 1500);
      await snap(ben, "usage", `usage-${tab}`, "with-data", `Usage & cost, ${tab} tab, as the owner, after chats by @ben and @cy`);
    });
  }
  await variants(ben, `${ROCKET}/usage`, { mode: M, area: "usage", name: "usage-overview", what: "Usage & cost (/usage)" });
  await attempt(ben, at("usage", "member-drilldown"), async () => {
    await ben.goto(`${ROCKET}/usage/members/${uid("cy")}`);
    await expect(ben.getByTestId("member-usage-page")).toBeVisible();
    await settle(ben, 1500);
    await snap(ben, "usage", "member-drilldown", "with-data", "One member's drill-down (/usage/members/<cy>) as the owner");
  });
  await attempt(cy, at("usage", "my-usage"), async () => {
    await cy.goto(`${ROCKET}/usage`);
    await expect(cy).toHaveURL(/\/usage\/me/);
    await settle(cy, 1500);
    await snap(cy, "usage", "my-usage", "with-data", "My usage (/usage -> /usage/me) as @cy, a member who chatted");
  });

  // Member budget banners for cy at 50 / 75 / 100 %, then the hard stop.
  const cySpent = async () =>
    (await api<{ usage?: { me?: { spent_usd: number } } }>(cy.request, "GET", `${ROCKET}/api/portal/state`)).usage?.me?.spent_usd ?? 0;
  const spent = await cySpent().catch(() => 0);
  if (!spent) gap(M, "budgets", "member-banner", "all", "cy has no recorded spend, so the member budget banners cannot be placed");
  const cents = (x: number) => Math.max(0.01, Math.ceil(x * 100) / 100);
  for (const [pct, budget, hard] of [
    [50, cents(spent / 0.6), false],
    [75, cents(spent / 0.85), false],
    [100, cents(spent * 0.9), false],
    [100, cents(spent * 0.9), true],
  ] as const) {
    if (!spent) break;
    const state = `member-${pct}${hard ? "-hard-stop" : ""}`;
    await attempt(cy, at("budgets", "member-banner", state), async () => {
      await api(ben.request, "PUT", `${ROCKET}/api/budgets/members/${uid("cy")}`, { monthly_usd: budget, hard_stop: hard });
      await cy.goto(`${ROCKET}/`);
      await settle(cy, 1200);
      await snap(cy, "budgets", "projects-member-banner", state, `@cy's Projects page with his member budget at ${pct}%${hard ? " and hard-stopped" : ""} ($${budget})`);
      if (pct === 100) {
        await cy.goto(`${ROCKET}/usage/me`);
        await settle(cy, 1200);
        await snap(cy, "budgets", "my-usage-member-banner", state, `My usage with the member budget at ${pct}%${hard ? " (hard stop)" : ""}`);
      }
      if (hard) {
        await cy.goto(`${ROCKET}/p/demo/chat`);
        await cy.getByRole("button", { name: "New chat", exact: true }).first().click();
        await expect(cy.getByTestId("chat-budget-notice")).toBeVisible();
        await snap(cy, "budgets", "chat", "member-hard-stop", "@cy's chat with his member budget hard-stopped: the composer is replaced");
      }
    });
  }
  await attempt(ben, at("budgets", "org-banner"), async () => {
    const st = await api<{ usage?: { org?: { spent_usd: number } } }>(ben.request, "GET", `${ROCKET}/api/portal/state`);
    const orgSpent = st.usage?.org?.spent_usd ?? 1;
    await api(ben.request, "PUT", `${ROCKET}/api/budgets/org`, { monthly_usd: cents(orgSpent / 0.8), hard_stop: false });
    await api(ben.request, "PUT", `${ROCKET}/api/projects/demo/budget`, { monthly_usd: cents(orgSpent / 0.8), hard_stop: false }).catch(() => undefined);
    await ben.goto(`${ROCKET}/`);
    await settle(ben, 1500);
    await snap(ben, "budgets", "projects-org-banner", "org-75", "Owner's Projects page with the org (and demo) at ~80% of budget: the org banner");
    await ben.goto(`${ROCKET}/usage?tab=budgets`);
    await settle(ben, 1500);
    await snap(ben, "budgets", "usage-budgets", "org-member-project", "Budgets tab with an org budget, a project budget and @cy's member budget");
    await ben.goto(`${ROCKET}/usage`);
    await settle(ben, 1500);
    await snap(ben, "budgets", "usage-overview", "org-75", "Usage overview with the org budget gauge and banner");
  });

  // ---- org settings, audit -------------------------------------------------
  await attempt(ben, at("org", "org-settings"), async () => {
    await ben.goto(`${ROCKET}/settings`);
    await expect(ben.getByTestId("org-general")).toBeVisible();
    await settle(ben, 1000);
    await snap(ben, "org", "org-settings", "owner", "Org settings (/settings) as the owner: General (models, keys), Ownership, Danger zone");
    const ownership = ben.getByTestId("org-ownership");
    await ownership.getByLabel("New owner").selectOption({ label: "Cy" });
    await ownership.getByRole("button", { name: "Transfer ownership" }).click();
    await expect(ben.getByTestId("transfer-dialog")).toBeVisible();
    await snap(ben, "org", "transfer-dialog", "open", "Transfer ownership: the typed-slug confirmation dialog", { viewportOnly: true });
    await ben.keyboard.press("Escape");
    await ben.getByTestId("org-danger-zone").getByRole("button", { name: "Delete organization" }).click();
    await expect(ben.getByTestId("delete-org-dialog")).toBeVisible();
    await snap(ben, "org", "delete-org-dialog", "open", "Delete organization: the typed-slug confirmation dialog", { viewportOnly: true });
    await ben.keyboard.press("Escape");
  });
  await variants(ben, `${ROCKET}/settings`, { mode: M, area: "org", name: "org-settings", what: "Org settings (/settings)" });
  await attempt(cy, at("org", "org-settings", "member"), async () => {
    await cy.goto(`${ROCKET}/settings`);
    await settle(cy, 1200);
    await snap(cy, "org", "org-settings", "member", "Org settings as a member (owner-only notice)");
  });
  await attempt(ben, at("org", "audit"), async () => {
    await ben.goto(`${ROCKET}/audit`);
    await expect(ben.getByTestId("audit-table")).toBeVisible();
    await settle(ben, 1500);
    await snap(ben, "org", "audit", "with-events", "Audit log (/audit) as the owner: invitations, grants, restriction, budgets");
  });
  await variants(ben, `${ROCKET}/audit`, { mode: M, area: "org", name: "audit", what: "Audit log (/audit)" });
  await attempt(cy, at("org", "audit", "member"), async () => {
    await cy.goto(`${ROCKET}/audit`);
    await settle(cy, 1200);
    await snap(cy, "org", "audit", "member", "/audit as a member (owners only)");
  });

  // ---- account pages ---------------------------------------------------------
  for (const [who, p] of [
    ["owner", ben],
    ["member", cy],
  ] as const) {
    await attempt(p, at("account", "account", who), async () => {
      await p.goto(`${base.origin}/account`);
      await settle(p, 1500);
      await snap(p, "account", "account", who, `Account page (/account) of ${who === "owner" ? "@ben (owner of two orgs)" : "@cy (member, budget hard-stopped)"}`);
    });
  }

  // ---- the instance administrator: admin page, reader view --------------
  await attempt(ada, at("admin", "admin"), async () => {
    await ada.goto(`${base.origin}/admin`);
    await settle(ada, 1500);
    await snap(ada, "admin", "admin", "default", "Instance admin page (/admin): settings check, orgs, users, admin audit");
  });
  await attempt(ada, at("admin", "account"), async () => {
    await ada.goto(`${base.origin}/account`);
    await settle(ada, 1200);
    await snap(ada, "account", "account", "instance-admin", "Account page of Ada (password account, instance admin)");
  });
  await attempt(ada, at("reader", "projects"), async () => {
    await ada.goto(`${ROCKET}/`);
    await expect(ada.getByTestId("reader-banner")).toBeVisible();
    await settle(ada, 1200);
    await snap(ada, "reader", "projects", "reader", "rocket's Projects page read by the instance admin (not a member): the reader banner");
    await ada.goto(`${ROCKET}/p/demo`);
    await settle(ada, 1200);
    await snap(ada, "reader", "overview", "reader", "A project's Overview in reader mode");
    await ada.goto(`${ROCKET}/p/demo/chat`);
    await settle(ada, 1200);
    await snap(ada, "reader", "chat", "reader", "Chat in reader mode");
    await ada.goto(`${ROCKET}/members`);
    await settle(ada, 1200);
    await snap(ada, "reader", "members", "reader", "Members in reader mode");
    await ada.goto(`${ROCKET}/usage`);
    await settle(ada, 1500);
    await snap(ada, "reader", "usage", "reader", "Usage & cost in reader mode");
    await ada.goto(`${ROCKET}/settings`);
    await settle(ada, 1200);
    await snap(ada, "reader", "org-settings", "reader", "Org settings in reader mode");
  });
  await attempt(ada, at("orgs", "org-picker", "admin"), async () => {
    await ada.goto(`${base.origin}/orgs`);
    await settle(ada, 1500);
    await snap(ada, "orgs", "org-picker", "instance-admin", "Org picker as the instance admin (member of acme only)");
  });
  await attempt(ben, at("edge", "not-found"), async () => {
    await ben.goto(`${ROCKET}/no-such-page`);
    await settle(ben, 1000);
    await snap(ben, "edge", "not-found", "org-host", "Unknown route on an org host");
    await ben.goto(`${base.origin}/no-such-page`);
    await settle(ben, 1000);
    await snap(ben, "edge", "not-found", "base-host", "Unknown route on the base host");
    await ben.goto(`${ROCKET}/p/no-such-project`);
    await settle(ben, 1500);
    await snap(ben, "edge", "not-found", "unknown-project", "Unknown project slug on an org host");
  });
  await attempt(ben, at("edge", "session-expired"), async () => {
    const restore = await (async () => {
      const handler = (route: import("@playwright/test").Route) =>
        route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ code: "login_required", detail: "Sign in" }) });
      await ben.route(/\/api\/projects$/, handler);
      return () => ben.unroute(/\/api\/projects$/, handler);
    })();
    try {
      await ben.goto(`${ROCKET}/`);
      await settle(ben, 2500);
      await snap(ben, "edge", "session-expired", "login-required", "A 401 login_required on the projects list (route intercept): where the SPA sends the browser");
    } finally {
      await restore();
    }
  });

  await deeC.close();
  await cyC.close();
  await benC.close();
});
