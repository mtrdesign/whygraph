import fs from "node:fs";
import path from "node:path";
import { expect, test, type Browser } from "@playwright/test";
import { env } from "../env";
import { call, configureStubLlm } from "../lib/usage";
import { expectSkipLink, tabUntil } from "../lib/ui";
import { base, createOrg, expectNoServerText, githubSignIn, orgUrl, signedIn } from "../lib/production";

// Onboarding on the production portal (M2f-3), on its own organization: a new org's
// 5-item checklist, two repositories imported at once, an invited user's one-time
// welcome banner, a member's read-only project settings, and the keyboard.
const ORG = orgUrl("onboard");

/** Hold the production fake scanner on each phase, so "Importing" outlives the page change. */
const DELAY = path.join(env.root, "prod-control", "delay");

async function as(browser: Browser, login: string) {
  const context = await browser.newContext({ baseURL: env.prodUrl });
  const page = await context.newPage();
  await githubSignIn(page, login);
  await signedIn(page);
  return { context, page };
}

/** Every `hint` in a JSON document (the key tail a configurer sees). */
function hints(value: unknown): unknown[] {
  if (Array.isArray(value)) return value.flatMap(hints);
  if (value && typeof value === "object") {
    return Object.entries(value).flatMap(([k, v]) => (k === "hint" ? [v] : hints(v)));
  }
  return [];
}

test("onboarding: checklist, two repositories at once, the welcome banner, a member's read-only settings, the keyboard", async ({
  browser,
}) => {
  test.setTimeout(300_000);
  const { context: benContext, page: ben } = await as(browser, "ben");
  await ben.goto(`${base.origin}/orgs/new`);
  await createOrg(ben, "Onboard", "onboard");

  // A new org's empty page is the 5-item checklist; nothing is done yet (GitHub counts once the App is connected).
  const checklist = ben.getByTestId("first-run-checklist");
  await expect(checklist).toContainText("No projects yet");
  await expect(checklist.getByTestId("first-run-list").getByRole("listitem")).toHaveCount(5);
  for (const id of ["github", "project", "llm_key", "invite", "agent"]) {
    await expect(checklist.getByTestId(`first-run-${id}`)).toBeVisible();
  }
  await expect(checklist.getByTestId("first-run-project")).toHaveAttribute("data-done", "false");
  await expectNoServerText(ben);
  await configureStubLlm(ben.request, ORG);

  // Import two repositories at once through the multi-select. Each scan is held for
  // a moment, so the list shows Importing before Ready.
  fs.writeFileSync(DELAY, "0.7\n");
  try {
    await ben.goto(`${ORG}/projects/new`);
    const connect = ben.getByTestId("github-connect").getByRole("button", { name: "Connect GitHub" });
    const demo = ben.getByRole("checkbox", { name: "Select ben/demo" });
    await expect(connect.or(demo)).toBeVisible();
    if (await connect.isVisible()) {
      await connect.click();
      await ben.getByRole("link", { name: "Continue as ben" }).click();
      await expect(ben).toHaveURL(new RegExp(`^${ORG}/projects/new`));
    }
    await demo.click();
    await ben.getByRole("checkbox", { name: "Select ben/notes" }).click();
    await ben.getByRole("button", { name: "Import 2 repositories" }).click();
    const results = ben.getByTestId("import-results");
    await expect(results.getByTestId("import-ben/demo")).toContainText("Started");
    await expect(results.getByTestId("import-ben/notes")).toContainText("Started");
    // Several repositories keep the results page (a single one goes straight to Configure).
    await expect(ben).toHaveURL(new RegExp(`^${ORG}/projects/new`));

    await ben.goto(`${ORG}/`);
    const cards = ["demo", "notes"].map((slug) => ben.getByTestId(`project-${slug}`));
    await expect(cards[0].or(cards[1]).filter({ hasText: "Importing" }).first()).toBeVisible();
    fs.rmSync(DELAY, { force: true });
    for (const card of cards) await expect(card).toContainText("Ready", { timeout: 60_000 });
  } finally {
    fs.rmSync(DELAY, { force: true });
  }

  // An invited user: dee is added as a member and sees the welcome banner once.
  await ben.goto(`${ORG}/members`);
  await ben.getByLabel("GitHub username").fill("dee");
  await ben.getByRole("button", { name: "Invite", exact: true }).click();
  await expect(ben.getByTestId("member-list")).toContainText("@dee");

  // Keyboard on Members: the skip link, then Tab reaches a member row's action.
  await ben.reload();
  await expectSkipLink(ben);
  const remove = ben.getByTestId("member-list").getByRole("listitem").filter({ hasText: "@dee" })
    .getByRole("button", { name: "Remove" });
  await tabUntil(ben, remove, 150);
  await expectNoServerText(ben);

  const { context: deeContext, page: dee } = await as(browser, "dee");
  await dee.goto(`${ORG}/`);
  const banner = dee.getByTestId("welcome-banner");
  await expect(banner).toContainText("You've been added to Onboard as a Member.");
  await banner.getByRole("button", { name: "Dismiss" }).click();
  await expect(banner).toHaveCount(0);
  await dee.reload();
  await expect(dee.getByTestId("project-demo")).toBeVisible();
  await expect(dee.getByTestId("welcome-banner")).toHaveCount(0);
  await dee.goto(`${ORG}/p/demo`);
  await expect(dee.getByTestId("welcome-banner")).toHaveCount(0);

  // A member sees the project's settings read-only: no key tails, no run cost.
  await dee.goto(`${ORG}/p/demo/settings`);
  await expect(dee.getByTestId("settings-read-only")).toBeVisible();
  // A key card says whether a key is set, never its tail (the stub key is `sk-e2e-stub`).
  for (const status of await dee.getByTestId("key-status").allInnerTexts()) expect(status).not.toMatch(/stub|sk-/i);
  await expect(dee.getByTestId("key-last-used")).toHaveCount(0);
  await expectNoServerText(dee);
  const config = await call<unknown>(dee.request, "GET", `${ORG}/api/projects/demo/config`);
  expect(hints(config).filter((h) => h !== null && h !== undefined)).toEqual([]);
  expect(JSON.stringify(config)).not.toContain("sk-e2e");
  await dee.goto(`${ORG}/p/demo/scans`);
  await expect(dee.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(dee.getByRole("columnheader", { name: "Cost" })).toHaveCount(0);
  const scans = await call<{ runs: Record<string, unknown>[] }>(dee.request, "GET", `${ORG}/api/projects/demo/scans`);
  expect(scans.runs.length).toBeGreaterThan(0);
  for (const run of scans.runs) expect(run).not.toHaveProperty("cost_usd");

  await deeContext.close();
  await benContext.close();
});
