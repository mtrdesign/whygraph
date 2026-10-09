import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { env } from "../env";
import { base, createOrg, githubSignIn, importRepo, orgUrl, signedIn } from "../lib/production";
import { attempt, gap, shoot, watch, type Mode, type ShotMeta } from "./lib/shoot";

// The linked-project screens (M2e), across both portals: the platform's consent
// page, the local checkout picker, a linked project's pages in both link states
// (ok, revoked), the platform's Connected portals lists, and - last, because it
// takes a repository away from the GitHub App - a project whose access was lost.
// Follows tests/linked.spec.ts, on ben/demo in its own org (orbit), so the local
// slug (demo) never collides with the local half's projects.

const ORG = "orbit";
const REPO = "ben/demo";
const SLUG = "demo";
const MACHINE = "audit-laptop.local";
const at = (mode: Mode, area: string, name: string, state = "default") => ({ mode, area, name, state });
const snap = (page: Page, mode: Mode, area: string, name: string, state: string, desc: string, opts?: Parameters<typeof shoot>[2]) =>
  shoot(page, { mode, area, name, state, desc } satisfies ShotMeta, opts);

function git(repo: string, ...args: string[]): string {
  return execFileSync("git", ["-C", repo, ...args], { stdio: "pipe", encoding: "utf8" });
}

function cloneFixture(): string {
  const repo = path.join(env.shared, SLUG);
  fs.rmSync(repo, { recursive: true, force: true });
  execFileSync("git", ["clone", "-q", path.join(env.githubRepos, `${REPO}.git`), repo], { stdio: "pipe" });
  git(repo, "config", "user.email", "cy@example.com");
  git(repo, "config", "user.name", "Cy");
  git(repo, "config", "commit.gpgsign", "false");
  git(repo, "remote", "set-url", "origin", `${env.githubUrl}/${REPO}.git`);
  return repo;
}

async function sharedRepos(page: Page): Promise<string[]> {
  return page.evaluate(async () => {
    const r = await fetch("/api/portal/repos", { headers: { "X-WhyGraph-Client": "1" } });
    if (!r.ok) throw new Error(`repos answered ${r.status}`);
    return ((await r.json()) as { repos: { path: string }[] }).repos.map((x) => x.path);
  });
}

/** One `whygraph_evidence_for` call over the local MCP endpoint (refreshes the link status). */
async function askEvidence(request: APIRequestContext): Promise<void> {
  await request.post(`${env.baseUrl}/mcp/${SLUG}`, {
    headers: { Accept: "application/json, text/event-stream", "Content-Type": "application/json" },
    data: {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: "whygraph_evidence_for", arguments: { path: "README.md", line_start: 1, line_end: 3 } },
    },
  });
}

async function settle(page: Page, ms = 1200): Promise<void> {
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => undefined);
  await page.waitForTimeout(ms);
}

test("linked project: both portals", async ({ page, browser, request }) => {
  test.setTimeout(45 * 60_000);
  watch(page);

  // The platform side: Ben imports the repo into orbit and adds Cy.
  const benC = await browser.newContext({ baseURL: env.prodUrl });
  const ben = await benC.newPage();
  watch(ben);
  await ben.goto("/signin");
  await githubSignIn(ben, "ben");
  await signedIn(ben);
  await ben.goto(`${base.origin}/orgs/new`);
  await createOrg(ben, "Orbit", ORG);
  await importRepo(ben, ORG, REPO);
  await ben.goto(`${orgUrl(ORG)}/members`);
  await ben.getByLabel("GitHub username").fill("cy");
  await ben.getByRole("button", { name: "Invite", exact: true }).click();
  await expect(ben.getByTestId("member-list")).toContainText("@cy");

  await attempt(ben, at("production", "linked", "use-with-agent"), async () => {
    await ben.goto(`${orgUrl(ORG)}/p/${SLUG}`);
    await settle(ben);
    await snap(ben, "production", "linked", "overview-use-with-agent", "default", "Platform project Overview (orbit/demo) with the 'Use with your agent' card, before any local portal is linked");
  });

  const repo = cloneFixture();
  await page.goto("/projects/new");
  await expect.poll(() => sharedRepos(page), { timeout: 70_000 }).toContain(repo);

  // The local side: the connect round trip, as Cy.
  let linked = false;
  await attempt(page, at("local", "linked", "connect"), async () => {
    await page.goto("/projects/new?source=platform");
    await page.getByLabel("Platform address").fill(env.prodUrl);
    await page.getByLabel("Machine name").fill(MACHINE);
    await snap(page, "local", "linked", "source-platform", "filled", "'From a platform' with the platform address and a machine name, before Connect");
    await page.getByRole("button", { name: "Connect" }).click();
    await expect(page).toHaveURL(new RegExp(`^${base.origin}/signin\\?next=`));
    await settle(page, 600);
    await snap(page, "production", "linked", "signin-for-connect", "default", "The platform's sign-in, reached from a local portal's Connect (next= the consent page)");
    await githubSignIn(page, "cy");
    await expect(page).toHaveURL(new RegExp(`^${base.origin}/connect`));
    await expect(page.getByTestId("connect-client")).toHaveText(MACHINE);
    await settle(page, 800);
    await snap(page, "production", "linked", "connect-consent", "default", "The platform's consent page (/connect): which machine asks, the project picker, Allow / Deny");
    await page.getByLabel("Project").selectOption({ label: `Orbit / ${SLUG}` });
    await page.getByRole("button", { name: "Allow" }).click();
    await expect(page).toHaveURL(new RegExp(`^${env.baseUrl}/projects/new\\?.*link=`));
    const picker = page.getByTestId("platform-picker");
    await expect(picker).toContainText(`${ORG}/${SLUG}`);
    await settle(page, 800);
    await snap(page, "local", "linked", "checkout-picker", "default", "Back on the local portal: the checkout picker for orbit/demo (no origin match, so 'Other repositories')");
    await picker
      .getByRole("radiogroup", { name: "Other repositories" })
      .locator("label")
      .filter({ has: page.getByText(repo, { exact: true }) })
      .getByRole("radio")
      .check();
    await page.getByRole("button", { name: "Link this checkout" }).click();
    await expect(page).toHaveURL(new RegExp(`/p/${SLUG}/init\\?step=initialize`));
    await settle(page, 800);
    await snap(page, "local", "linked", "wizard-initialize", "default", "A linked project's wizard: Configure skipped (the platform owns config), Initialize");
    await page.getByRole("checkbox", { name: /Claude Code/ }).check();
    await page.getByRole("button", { name: "Initialize", exact: true }).click();
    await expect(page.getByTestId("init-done")).toBeVisible();
    await page.getByRole("button", { name: "Continue to first scan" }).click();
    await page.getByRole("button", { name: "Start first scan" }).click();
    await expect(page.getByText("First scan complete")).toBeVisible({ timeout: 60_000 });
    await settle(page, 600);
    await snap(page, "local", "linked", "wizard-scan", "done", "A linked project's first scan done (CodeGraph only)");
    await page.getByRole("button", { name: "Open project", exact: true }).click();
    await expect(page.getByTestId("link-notice")).toHaveAttribute("data-status", "ok");
    linked = true;
  });

  if (linked) {
    const pages: [string, string, string][] = [
      ["", "overview", "Linked project Overview (/p/demo): the link notice 'Linked to orbit/demo', platform panel"],
      ["/explorer", "explorer", "Linked project Explorer (local CodeGraph, evidence from the platform)"],
      ["/scans", "scan-history", "Linked project scan history (CodeGraph-only scans)"],
      ["/chat", "chat", "Linked project Chat (chat is not offered for linked projects)"],
      ["/settings", "project-settings", "Linked project settings: General (platform name), Git hooks, Agents, Danger zone (managed on the platform)"],
    ];
    for (const [suffix, name, desc] of pages) {
      await attempt(page, at("local", "linked", name, "ok"), async () => {
        await page.goto(`/p/${SLUG}${suffix}`);
        await settle(page, 1500);
        await snap(page, "local", "linked", name, "link-ok", desc);
      });
    }
    await attempt(page, at("local", "linked", "projects", "ok"), async () => {
      await page.goto("/");
      await settle(page, 1200);
      await snap(page, "local", "linked", "projects", "link-ok", "Local Projects page with the linked project's card (link ok) among the local ones");
    });
    await attempt(page, at("production", "account", "connected-portals"), async () => {
      await page.goto(`${base.origin}/account`);
      await expect(page.getByTestId("my-connections")).toBeVisible();
      await settle(page, 1000);
      await snap(page, "production", "account", "account", "connected-portal", "@cy's Account page on the platform with one connected local portal (My connections)");
    });
    await attempt(ben, at("production", "linked", "project-connections"), async () => {
      await ben.goto(`${orgUrl(ORG)}/p/${SLUG}/settings`);
      await expect(ben.getByTestId("project-connections")).toBeVisible();
      await settle(ben, 1000);
      await snap(ben, "production", "linked", "project-settings-connections", "default", "Platform project settings with the Connected portals list (cy's machine)");
      await ben.goto(`${orgUrl(ORG)}/usage?tab=machines`);
      await settle(ben, 1500);
      await snap(ben, "production", "usage", "usage-machines", "orbit-linked", "orbit's Usage & cost, Machines tab after a local portal was linked");
      // Revoke from the platform.
      const row = ben.getByTestId("project-connections").getByTestId("connection-row").filter({ hasText: MACHINE });
      await ben.goto(`${orgUrl(ORG)}/p/${SLUG}/settings`);
      await row.getByRole("button", { name: "Revoke" }).click();
      await expect(ben.getByTestId("project-connections")).toContainText("No local portal is connected to this project.");
      await settle(ben, 600);
      await snap(ben, "production", "linked", "project-settings-connections", "revoked", "Platform project settings after the admin revoked the connection");
    });
    await askEvidence(request).catch(() => undefined);
    await attempt(page, at("local", "linked", "revoked"), async () => {
      await page.goto("/");
      await expect(page.getByTestId("link-notice")).toHaveAttribute("data-status", "revoked");
      await settle(page, 1000);
      await snap(page, "local", "linked", "projects", "link-revoked", "Local Projects page: the linked card says 'Access revoked (Revoked by an admin)'");
      await page.goto(`/p/${SLUG}`);
      await settle(page, 1200);
      await snap(page, "local", "linked", "overview", "link-revoked", "Linked project Overview after revocation");
      await page.goto(`/p/${SLUG}/settings`);
      await page.getByRole("button", { name: "Remove from this machine" }).click();
      await expect(page.getByTestId("remove-dialog")).toBeVisible();
      await snap(page, "local", "linked", "remove-dialog", "open", "'Remove from this machine' dialog of a linked project", { viewportOnly: true });
      await page.getByTestId("remove-dialog").getByRole("button", { name: "Remove from this machine" }).click();
      await expect(page.getByTestId("remove-done")).toBeVisible();
      await settle(page, 600);
      await snap(page, "local", "linked", "remove-dialog", "done-revoke-failed", "The remove dialog done: the token could not be revoked (already revoked), pointer to the platform", { viewportOnly: true });
    });
  } else {
    gap("local", "linked", "linked project pages", "all", "the connect round trip failed, so no linked project exists");
  }
  gap("local", "linked", "link states", "unreachable / update_required / removed / access_lost", "not simulated: only ok and revoked are reachable with the harness");
  gap("local", "linked", "/link deep link", "default", "the platform's 'Use with your agent' deep link (/link?platform=...) was not followed; the source-platform form covers the same connect");

  // Last: the GitHub App loses ben/demo, so every org that imported it shows access lost.
  await attempt(ben, at("production", "projects", "access-lost"), async () => {
    const res = await fetch(`${env.githubUrl}/_fake/remove-repo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo: REPO }),
    });
    expect(res.status).toBe(200);
    await ben.waitForTimeout(3000);
    await ben.goto(`${orgUrl("rocket")}/`);
    await settle(ben, 1500);
    await snap(ben, "production", "projects", "projects", "access-lost", "rocket's Projects page after the GitHub App lost access to ben/demo (installation_repositories.removed)");
    await ben.goto(`${orgUrl("rocket")}/p/demo`);
    await settle(ben, 1500);
    await snap(ben, "production", "project", "overview", "access-lost", "Overview of demo after the GitHub App lost access to the repository");
  });
  await benC.close();
});
