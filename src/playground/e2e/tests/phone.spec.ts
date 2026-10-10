import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { env } from "../env";
import { themeRepos } from "../lib/fixtures";
import { horizontalOverflow } from "../lib/overflow";
import { base, githubSignIn, orgUrl, signedIn } from "../lib/production";
import { call, configureStubLlm, STUB_REPLY } from "../lib/usage";

// The main routes of both portals at a phone's width (390x844, light): none may
// scroll horizontally (except an element marked `data-scroll-x`, an opted-in
// scroller) and each has an h1, visible or `sr-only`. M2f-3 plan section 4.20.
//
// Data: local - `notes-light`, the project `main-flow` adds and scans in the
// `light` pass, plus a chat session this spec creates through the fake LLM;
// production - ben signed in with GitHub, organization `comet` and its project
// `demo` (imported by `production.spec.ts`), and the base host.
//
const PHONE = { width: 390, height: 844 };
const { notes } = themeRepos("light");
const project = `/p/${notes.slug}`;
const comet = orgUrl("comet");

/** Let the page settle, then collect what is wrong with it at this width. */
async function problems(page: Page): Promise<string[]> {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await page.waitForTimeout(400);
  const out: string[] = [];
  try {
    await expect(page.locator("h1").first()).toBeAttached({ timeout: 5_000 });
  } catch {
    out.push("no h1");
  }
  for (const o of await horizontalOverflow(page)) out.push(`overflow: ${o}`);
  return out;
}

async function check(page: Page, name: string, url: string): Promise<void> {
  await page.goto(url);
  expect(await problems(page), `${name} at ${url}`).toEqual([]);
}

test.describe("local portal at 390px", () => {
  let session = "";
  let run = "";

  test.beforeAll(async ({ browser }) => {
    // Desktop width, like the other specs; the tests below are the phone. The `light`
    // pass already left one chat (usage.spec.ts), whose session also reads "New chat".
    const context = await browser.newContext({ baseURL: env.baseUrl, viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    await page.goto("/");
    await configureStubLlm(page.request, env.baseUrl);
    await page.goto(`${project}/chat`);
    await page.getByRole("button", { name: "New chat" }).first().click();
    await page.getByPlaceholder("Ask about this repository…").fill("hello from the phone project");
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByText(STUB_REPLY).first()).toBeVisible();
    session = page.url();
    const scans = await call<{ runs: { id: string }[] }>(page.request, "GET", `${env.baseUrl}/api/projects/${notes.slug}/scans`);
    run = scans.runs[0]?.id ?? "";
    await context.close();
  });

  const routes: [string, () => string][] = [
    ["local /", () => "/"],
    ["local /projects/new", () => "/projects/new"],
    ["local /settings", () => "/settings"],
    ["local /usage", () => "/usage"],
    ["local project overview", () => project],
    ["local explorer", () => `${project}/explorer`],
    ["local chat (new)", () => `${project}/chat`],
    ["local chat (session)", () => session],
    ["local scans", () => `${project}/scans`],
    ["local scan run", () => `${project}/scans/${run}`],
    ["local project settings", () => `${project}/settings`],
    ["local wizard (configure)", () => `${project}/init?step=configure`],
  ];
  for (const [name, url] of routes) {
    test(name, async ({ page }) => {
      await check(page, name, url());
    });
  }

  test("local navigation sheet is 240px wide", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Open navigation" }).click();
    const sheet = page.locator('[data-slot="sheet-content"]');
    await expect(sheet).toBeVisible();
    await page.waitForTimeout(500); // the slide-in
    expect(Math.round((await sheet.boundingBox())!.width)).toBe(240);
    expect(await horizontalOverflow(page)).toEqual([]);
  });
});

test.describe("production portal at 390px", () => {
  let context: BrowserContext;
  let page: Page;

  test.beforeAll(async ({ browser }) => {
    context = await browser.newContext({ baseURL: env.prodUrl, viewport: PHONE, colorScheme: "light" });
    page = await context.newPage();
    await githubSignIn(page, "ben");
    await signedIn(page);
  });
  test.afterAll(async () => {
    await context.close();
  });

  const routes: [string, string][] = [
    ["production /", `${comet}/`],
    ["production /members", `${comet}/members`],
    ["production /audit", `${comet}/audit`],
    ["production /usage", `${comet}/usage`],
    ["production project overview", `${comet}/p/demo`],
    ["production project settings", `${comet}/p/demo/settings`],
    ["production base /orgs", `${base.origin}/orgs`],
    ["production base /account", `${base.origin}/account`],
  ];
  for (const [name, url] of routes) {
    test(name, async () => {
      await check(page, name, url);
    });
  }
});
