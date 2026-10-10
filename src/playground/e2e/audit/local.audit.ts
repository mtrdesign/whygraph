import fs from "node:fs";
import path from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { env } from "../env";
import { attempt, gap, keepShell, shoot, variants, watch, type ShotMeta } from "./lib/shoot";
import { api, chat, COMPOSER, configureAuditLlm, scanControl } from "./lib/seed";

// The local-mode half of the screenshot audit: the first run, the add-project
// wizard, several projects in different states, a project's pages, Chat with a
// tool-calling turn, scans of every outcome, settings, Usage & cost, edge pages.
// One long test: each screen is an `attempt`, so a broken screen is recorded as a
// gap (gaps.md) instead of ending the run.

const M = "local" as const;
const ctl = scanControl();
const repo = (slug: string) => path.join(env.shared, slug);
const at = (area: string, name: string, state = "default") => ({ mode: M, area, name, state });
const snap = (page: Page, area: string, name: string, state: string, desc: string, opts?: Parameters<typeof shoot>[2]) =>
  shoot(page, { mode: M, area, name, state, desc } satisfies ShotMeta, opts);

/** Add a repo through the wizard (list pick), keep the defaults, Initialize with Claude Code. */
async function addAndInit(page: Page, slug: string): Promise<void> {
  await page.goto("/projects/new");
  await page.getByRole("radio", { name: new RegExp(`${slug}\\b`) }).check();
  await expect(page.getByText("Ready to add")).toBeVisible();
  await page.getByRole("button", { name: "Add project" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${slug}/init\\?step=configure`));
  await page.getByRole("button", { name: "Save and continue" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${slug}/init\\?step=setup`));
  await page.getByRole("checkbox", { name: /Claude Code/ }).check();
  await expect(page.getByTestId("init-preview")).toBeVisible();
  await page.getByRole("button", { name: "Initialize", exact: true }).click();
  await expect(page.getByTestId("init-done")).toBeVisible();
  await page.getByRole("button", { name: "Continue to first scan" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${slug}/init\\?step=configure`));
}

async function firstScanDone(page: Page, slug: string): Promise<void> {
  // The first Initialize queued the first scan: no "Start first scan" click.
  await expect(page.getByText("First scan complete")).toBeVisible({ timeout: 60_000 });
  await page.getByRole("button", { name: "Open project" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${slug}$`));
}

/** Quick rescan from the project page; resolves on the new run's page with its id. */
async function rescan(page: Page, slug: string): Promise<number> {
  await page.goto(`/p/${slug}`);
  await page.getByRole("button", { name: "Rescan", exact: true }).first().click();
  await page.getByRole("menuitem", { name: "Quick rescan" }).click();
  await page.waitForURL(`**/p/${slug}/scans/*`);
  return Number(new URL(page.url()).pathname.split("/").pop());
}

async function waitRunEnd(page: Page, text: RegExp | string): Promise<void> {
  await expect(page.getByTestId("run-result")).toContainText(text, { timeout: 90_000 });
}

test("local mode: every screen", async ({ page }) => {
  test.setTimeout(60 * 60_000);
  watch(page);

  // ---- first run -----------------------------------------------------------
  await attempt(page, at("onboarding", "setup"), async () => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/setup$/);
    await expect(page.getByRole("heading", { name: "Welcome to WhyGraph" })).toBeVisible();
    await snap(page, "onboarding", "setup", "empty", "First-run setup (/ redirects to /setup on a fresh portal), name field empty");
    await page.getByLabel("Your name").fill("Ada Lovelace");
    await snap(page, "onboarding", "setup", "filled", "First-run setup with a name typed, before Continue");
  });
  // The rest needs the user, so this one is not an `attempt`.
  if (!page.url().includes("/setup")) await page.goto("/setup");
  if (await page.getByLabel("Your name").isVisible().catch(() => false)) {
    await page.getByLabel("Your name").fill("Ada Lovelace");
    await page.getByRole("button", { name: "Continue" }).click();
  }
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();

  await attempt(page, at("projects", "projects", "empty"), async () => {
    await expect(page.getByText("No projects yet")).toBeVisible();
    await snap(page, "projects", "projects", "empty", "Projects page right after setup: no projects yet");
  });
  await variants(page, "/", { mode: M, area: "projects", name: "projects", what: "Projects page (/)" }, { states: ["loading", "error", "forbidden"] });

  await attempt(page, at("phone-nav", "projects", "nav-open"), async () => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
    await snap(page, "phone-nav", "projects", "nav-open", "Phone width: the navigation sheet opened with the header's menu button", {
      widths: ["phone"],
      viewportOnly: true,
      before: async (p) => {
        if (!(await p.getByRole("navigation", { name: "Main" }).isVisible())) {
          await p.getByRole("button", { name: "Open navigation" }).click();
        }
        await p.waitForTimeout(400);
      },
    });
    await page.keyboard.press("Escape");
  });

  await attempt(page, at("usage", "usage", "empty"), async () => {
    await page.goto("/usage");
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await snap(page, "usage", "usage-overview", "empty", "Usage & cost before any LLM call (/usage)");
  });
  await attempt(page, at("settings", "global-settings", "no-keys"), async () => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await page.waitForTimeout(800);
    await snap(page, "settings", "global-settings", "no-keys", "Global Settings (/settings) on a fresh portal: no keys, default models");
  });

  // ---- the add-project wizard (notes) --------------------------------------
  await attempt(page, at("add-project", "source-local", "folder-list"), async () => {
    await page.goto("/projects/new");
    await expect(page.getByRole("radio", { name: /notes\b/ })).toBeVisible();
    await snap(page, "add-project", "source-local", "folder-list", "Add project, local source: the shared folder's repos listed as radios");
    await page.getByRole("radio", { name: /notes\b/ }).check();
    await expect(page.getByText("Ready to add")).toBeVisible();
    await snap(page, "add-project", "source-local", "selected", "Add project: notes picked from the list, 'Ready to add'");
  });
  await attempt(page, at("add-project", "source-local", "not-shared"), async () => {
    await page.goto("/projects/new");
    await page.getByLabel("Or enter a path").fill(env.outside);
    await page.getByRole("button", { name: "Check" }).click();
    await expect(page.getByTestId("not-shared-alert")).toBeVisible();
    await snap(page, "add-project", "source-local", "not-shared", "Add project: a typed path outside the shared folders - the not-shared alert with the `whygraph up --add-folder` command");
  });
  await attempt(page, at("add-project", "source-local", "not-a-repo"), async () => {
    fs.mkdirSync(path.join(env.shared, "plain-folder"), { recursive: true });
    await page.goto("/projects/new");
    await page.getByLabel("Or enter a path").fill(path.join(env.shared, "plain-folder"));
    await page.getByRole("button", { name: "Check" }).click();
    await page.waitForTimeout(1500);
    await snap(page, "add-project", "source-local", "not-a-repo", "Add project: a typed path inside the shared folder that is not a git repository");
  });
  await attempt(page, at("add-project", "source-local", "missing-path"), async () => {
    await page.goto("/projects/new");
    await page.getByLabel("Or enter a path").fill(path.join(env.shared, "does-not-exist"));
    await page.getByRole("button", { name: "Check" }).click();
    await page.waitForTimeout(1500);
    await snap(page, "add-project", "source-local", "missing-path", "Add project: a typed path that does not exist");
  });
  await attempt(page, at("add-project", "source-platform", "empty"), async () => {
    await page.goto("/projects/new?source=platform");
    await expect(page.getByLabel("Platform address")).toBeVisible();
    await snap(page, "add-project", "source-platform", "empty", "Add project, 'From a platform' source (/projects/new?source=platform): the connect form, empty");
  });
  await attempt(page, at("add-project", "source-platform", "unreachable"), async () => {
    await page.goto("/projects/new?source=platform");
    await page.getByLabel("Platform address").fill("https://unreachable.invalid");
    await page.getByLabel("Machine name").fill("audit-laptop");
    await page.getByRole("button", { name: "Connect" }).click();
    await page.waitForTimeout(4000);
    await snap(page, "add-project", "source-platform", "unreachable", "'From a platform' with an address that does not resolve, after Connect");
  });

  const wizard = "add-project";
  await attempt(page, at(wizard, "configure"), async () => {
    await page.goto("/projects/new");
    await page.getByRole("radio", { name: /notes\b/ }).check();
    await page.getByRole("button", { name: "Add project" }).click();
    await expect(page).toHaveURL(/\/p\/notes\/init\?step=configure/);
    await page.waitForTimeout(800);
    await snap(page, wizard, "step-configure", "default", "Wizard step 1, Configure (/p/notes/init?step=configure) right after Add project");
    await page.getByRole("button", { name: "Save and continue" }).click();
    await expect(page).toHaveURL(/step=setup/);
    await page.waitForTimeout(800);
    await snap(page, wizard, "step-initialize", "no-agent", "Wizard step 2, Initialize: no agent ticked yet");
    await page.getByRole("checkbox", { name: /Claude Code/ }).check();
    await expect(page.getByTestId("init-preview")).toBeVisible();
    await page.waitForTimeout(500);
    await snap(page, wizard, "step-initialize", "preview", "Wizard step 2 with Claude Code ticked: the per-file init preview");
    // Initialize queues the first scan: slow the fake scanner so it is still running on the next step.
    ctl.delay(2.5);
    await page.getByRole("button", { name: "Initialize", exact: true }).click();
    await expect(page.getByTestId("init-done")).toBeVisible();
    await snap(page, wizard, "step-initialize", "done", "Wizard step 2 after Initialize: what was written");
    await page.getByRole("button", { name: "Continue to first scan" }).click();
    await expect(page).toHaveURL(/step=configure/);
    await page.waitForTimeout(800);
    await snap(page, wizard, "step-scan", "ready", "Wizard first-scan step (?step=configure after Initialize): the first scan queued automatically by Initialize, settings below");
  });
  await attempt(page, at(wizard, "step-scan", "running"), async () => {
    if (!/step=configure/.test(page.url())) await page.goto("/p/notes/init?step=configure");
    await expect(page.getByTestId("scan-progress")).toBeVisible();
    await page.waitForTimeout(3000);
    await snap(page, wizard, "step-scan", "running", "Wizard step 3 mid-run: live first-scan progress (fake scanner slowed to 2.5 s / tick)", { viewportOnly: true });
    ctl.delay(null);
    await expect(page.getByText("First scan complete")).toBeVisible({ timeout: 90_000 });
    await page.waitForTimeout(800);
    await snap(page, wizard, "step-scan", "done", "Wizard step 3 done: 'First scan complete' and the cost card (the fake scanner records no commits, so 'Nothing to describe')");
    await page.getByRole("button", { name: "Open project" }).click();
    await expect(page).toHaveURL(/\/p\/notes$/);
    await page.waitForTimeout(800);
    await snap(page, "project", "overview", "first-visit", "Project Overview (/p/notes) right after the wizard");
  });
  ctl.delay(null);

  // ---- more projects, in different states ----------------------------------
  for (const slug of ["billing", "web", "legacy"]) {
    await attempt(page, at("seed", slug), async () => {
      await addAndInit(page, slug);
      await firstScanDone(page, slug);
    });
  }
  await attempt(page, at("seed", "docs-site"), async () => {
    await page.goto("/projects/new");
    await page.getByRole("radio", { name: /docs-site\b/ }).check();
    await page.getByRole("button", { name: "Add project" }).click();
    await expect(page).toHaveURL(/\/p\/docs-site\/init\?step=configure/);
  });

  // ---- scans: running (with a follow-up), ok, failed, cancelled -----------
  await attempt(page, at("scans", "scan-run", "running"), async () => {
    ctl.delay(2.5);
    await rescan(page, "notes");
    await expect(page.getByTestId("phase-1")).toHaveAttribute("data-status", "running");
    await page.waitForTimeout(2500);
    await snap(page, "scans", "scan-run", "running", "Scan run page (/p/notes/scans/<id>) while a quick rescan runs: phases and task bars", { viewportOnly: true });
    await page.getByRole("button", { name: "Rescan", exact: true }).first().click();
    await page.getByRole("menuitem", { name: "Quick rescan" }).click();
    await expect(page.getByTestId("followup")).toBeVisible();
    await snap(page, "scans", "scan-run", "running-followup", "The same running run after a second Rescan click: the queued follow-up notice", { viewportOnly: true });
    await page.getByRole("button", { name: "Rescan", exact: true }).first().click();
    await page.waitForTimeout(500);
    await snap(page, "scans", "rescan-menu", "open", "The Rescan menu (Quick / Full rescan) open on a run page", { widths: ["desktop"], viewportOnly: true });
    await page.keyboard.press("Escape");
    ctl.delay(null);
    await waitRunEnd(page, "Finished in");
    await page.waitForTimeout(500);
    await snap(page, "scans", "scan-run", "ok", "Scan run page after the quick rescan succeeded");
  });
  ctl.delay(null);
  await attempt(page, at("scans", "scan-run", "failed"), async () => {
    // Let any follow-up finish first.
    await page.waitForTimeout(4000);
    ctl.fail(true);
    await rescan(page, "billing");
    await waitRunEnd(page, "The scan failed");
    const toggle = page.getByTestId("run-log").getByRole("button", { name: /Log/ });
    if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
    await page.waitForTimeout(500);
    await snap(page, "scans", "scan-run", "failed", "Scan run page of a failed run (control/fail): the error and the opened log");
  });
  ctl.fail(false);
  await attempt(page, at("scans", "scan-run", "cancelled"), async () => {
    ctl.delay(2.5);
    const run = await rescan(page, "web");
    await expect(page.getByTestId("phase-1")).toHaveAttribute("data-status", "running");
    await page.getByTestId("cancel-run").click();
    await expect(page.getByTestId("cancel-run-dialog")).toContainText(`Cancel scan #${run}?`);
    await snap(page, "scans", "cancel-dialog", "open", "The Cancel scan confirmation dialog over a running run", { viewportOnly: true });
    await page.getByTestId("cancel-run-dialog").getByRole("button", { name: "Cancel scan" }).click();
    await waitRunEnd(page, "You cancelled this run");
    ctl.delay(null);
    await snap(page, "scans", "scan-run", "cancelled", "Scan run page of a run cancelled by the user");
  });
  ctl.delay(null);
  await attempt(page, at("scans", "scan-run", "full-rescan-describe"), async () => {
    // A Full rescan runs the fake's phase 4 (LLM descriptions).
    ctl.delay(1.5);
    await page.goto("/p/notes");
    await page.getByRole("button", { name: "Rescan", exact: true }).first().click();
    await page.getByRole("menuitem", { name: "Full rescan" }).click();
    await page.waitForURL("**/p/notes/scans/*");
    await page.waitForTimeout(1500);
    await snap(page, "scans", "scan-run", "full-rescan-running", "A Full rescan running (four phases incl. LLM descriptions)", { viewportOnly: true });
    ctl.delay(null);
    await waitRunEnd(page, /Finished in|failed/);
    await snap(page, "scans", "scan-run", "full-rescan-done", "The Full rescan's run page once it ended");
  });
  ctl.delay(null);

  await attempt(page, at("scans", "scan-history"), async () => {
    await page.goto("/p/notes/scans");
    await expect(page.getByTestId("scan-history")).toBeVisible();
    await page.waitForTimeout(500);
    await snap(page, "scans", "scan-history", "several-runs", "Scan history of notes (/p/notes/scans): first scan, ok and full rescans");
    await page.goto("/p/billing/scans");
    await expect(page.getByTestId("scan-history")).toBeVisible();
    await snap(page, "scans", "scan-history", "with-failed", "Scan history of billing: first scan ok, latest failed");
    await page.goto("/p/web/scans");
    await expect(page.getByTestId("scan-history")).toBeVisible();
    await snap(page, "scans", "scan-history", "with-cancelled", "Scan history of web: a cancelled run");
  });
  await variants(page, "/p/notes/scans", { mode: M, area: "scans", name: "scan-history", what: "Scan history (/p/notes/scans)" }, {
    match: keepShell([/^\/api\/projects\/notes$/, /^\/api\/projects$/]),
  });
  await attempt(page, at("scans", "scan-run", "not-found"), async () => {
    await page.goto("/p/notes/scans/99999");
    await page.waitForTimeout(2500);
    await snap(page, "scans", "scan-run", "not-found", "A scan run id that does not exist (/p/notes/scans/99999)");
  });

  // The legacy checkout disappears from disk: the project is "missing".
  fs.rmSync(repo("legacy"), { recursive: true, force: true });

  await attempt(page, at("projects", "projects", "several"), async () => {
    // One project scanning while the page is captured.
    ctl.delay(3);
    await rescan(page, "web");
    await page.goto("/");
    await expect(page.getByTestId("project-billing")).toContainText("Scan failed");
    await page.waitForTimeout(1500);
    await snap(page, "projects", "projects", "several", "Projects page with 5 projects: notes ok, billing last scan failed, web scanning, legacy folder missing, docs-site not initialized", { viewportOnly: false });
  });
  ctl.delay(null);
  await attempt(page, at("projects", "project-switcher", "open"), async () => {
    await page.goto("/p/notes");
    await page.getByRole("button", { name: "Switch project" }).click();
    await page.waitForTimeout(400);
    await snap(page, "projects", "project-switcher", "open", "The sidebar's project switcher open on a project page", { widths: ["desktop"], viewportOnly: true });
    await page.keyboard.press("Escape");
  });

  // ---- project edge states -------------------------------------------------
  await attempt(page, at("project", "overview", "not-initialized"), async () => {
    await page.goto("/p/docs-site");
    await page.waitForTimeout(1500);
    await snap(page, "project", "overview", "not-initialized", "Overview of docs-site, added but never initialized (/p/docs-site)");
    await page.goto("/p/docs-site/explorer");
    await page.waitForTimeout(1500);
    await snap(page, "project", "explorer", "not-initialized", "Explorer of a project that is not initialized");
  });
  await attempt(page, at("project", "overview", "folder-missing"), async () => {
    await page.goto("/p/legacy");
    await page.waitForTimeout(2000);
    await snap(page, "project", "overview", "folder-missing", "Overview of legacy after its checkout was deleted from disk");
    await page.goto("/p/legacy/chat");
    await page.waitForTimeout(1500);
    await snap(page, "project", "chat", "folder-missing", "Chat of a project whose folder is missing (edge-state notice)");
  });

  // ---- a project's pages (notes) -------------------------------------------
  await attempt(page, at("project", "overview"), async () => {
    await page.goto("/p/notes");
    await expect(page.getByTestId("mcp-url")).toBeVisible();
    await page.waitForTimeout(800);
    await snap(page, "project", "overview", "default", "Project Overview of notes after several scans");
  });
  await variants(page, "/p/notes", { mode: M, area: "project", name: "overview", what: "Project Overview (/p/notes)" }, {
    match: keepShell([/^\/api\/projects$/]),
  });

  await attempt(page, at("explorer", "explorer", "graph"), async () => {
    await page.goto("/p/notes/explorer");
    await expect(page.getByTestId("tree")).toBeVisible();
    await page.waitForTimeout(1500);
    await snap(page, "explorer", "explorer", "nothing-selected", "Explorer (/p/notes/explorer): tree, graph canvas, empty detail panel");
    await page.getByTestId("tree").getByText("src", { exact: true }).click();
    await page.getByTestId("tree").getByText("notes.py", { exact: true }).click();
    await page.getByTestId("tree").getByText("notes_main", { exact: true }).click();
    await expect(page).toHaveURL(/node=/);
    await page.waitForTimeout(1200);
    await snap(page, "explorer", "node-detail", "relationships", "Explorer with notes_main selected: Relationships tab");
    for (const tab of ["Rationale", "Evidence", "History"]) {
      await page.getByRole("tab", { name: tab }).click();
      await page.waitForTimeout(1500);
      await snap(page, "explorer", "node-detail", `${tab.toLowerCase()}-no-llm`, `Explorer node detail, ${tab} tab (no LLM key configured yet; the fake scan recorded no commits)`);
    }
  });
  await variants(page, "/p/notes/explorer", { mode: M, area: "explorer", name: "explorer", what: "Explorer (/p/notes/explorer)" }, {
    match: keepShell([/^\/api\/projects\/notes$/, /^\/api\/projects$/]),
  });
  await attempt(page, at("explorer", "command-palette", "open"), async () => {
    await page.goto("/p/notes/explorer");
    await expect(page.getByTestId("tree")).toBeVisible();
    await page.keyboard.press("ControlOrMeta+k");
    await page.waitForTimeout(500);
    await page.keyboard.type("main");
    await page.waitForTimeout(1200);
    await snap(page, "explorer", "command-palette", "open", "Command palette (Ctrl/Cmd+K) open on the Explorer with 'main' typed", { viewportOnly: true });
    await page.keyboard.press("Escape");
  });

  // ---- chat, before any LLM key -------------------------------------------
  await attempt(page, at("chat", "chat", "no-session"), async () => {
    await page.goto("/p/notes/chat");
    await expect(page.getByRole("button", { name: "New chat", exact: true }).first()).toBeVisible();
    await page.waitForTimeout(800);
    await snap(page, "chat", "chat", "no-session", "Chat (/p/notes/chat) with no session: 'Select a chat, or start a new one'");
    await page.getByRole("button", { name: "New chat", exact: true }).first().click();
    await page.waitForTimeout(1500);
    await snap(page, "chat", "chat", "no-llm-key", "A new chat before any LLM key is configured");
  });

  // ---- the scripted LLM: chats and usage -----------------------------------
  await configureAuditLlm(page.request, env.baseUrl);
  await attempt(page, at("chat", "chat", "new-empty"), async () => {
    await page.goto("/p/notes/chat");
    await page.getByRole("button", { name: "New chat", exact: true }).first().click();
    await expect(page.getByPlaceholder(COMPOSER)).toBeVisible();
    await page.waitForTimeout(800);
    await snap(page, "chat", "chat", "new-empty", "A new, empty chat with the composer (LLM configured)");
  });
  await attempt(page, at("chat", "chat", "conversation-chart"), async () => {
    await chat(page, "/p/notes", "Which symbols are the largest? Chart it please. [[chart]]", "The chart above shows lines per symbol.");
    await page.waitForTimeout(1500);
    await snap(page, "chat", "chat", "conversation-chart", "A finished turn: search_symbols + run_graph_stats tool cards, a render_chart bar chart, a Markdown answer (scripted LLM)");
    // Expand the tool cards.
    const cards = page.getByRole("button", { name: /search_symbols|run_graph_stats|Searched|Ran/ });
    const n = await cards.count();
    for (let i = 0; i < Math.min(n, 3); i++) await cards.nth(i).click().catch(() => undefined);
    await page.waitForTimeout(600);
    await snap(page, "chat", "chat", "tool-cards-expanded", "The same turn with its tool cards expanded (best effort: the buttons matching the tool names)");
  });
  await attempt(page, at("chat", "chat", "plain"), async () => {
    await chat(page, "/p/notes", "What does this repository do?", "This repository is small");
    await page.getByPlaceholder(COMPOSER).fill("And who wrote it?");
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByText("This repository is small").nth(1)).toBeVisible({ timeout: 30_000 });
    await page.waitForTimeout(800);
    await snap(page, "chat", "chat", "conversation-plain", "A two-turn plain-text conversation; the session list holds several chats");
  });
  await attempt(page, at("chat", "chat", "streaming"), async () => {
    await chat(page, "/p/notes", "Explain slowly [[slow]]");
    await page.waitForTimeout(2500);
    await snap(page, "chat", "chat", "streaming", "A turn in flight (the scripted LLM streams for ~20 s)", { viewportOnly: true });
    await expect(page.getByText("This repository is small").first()).toBeVisible({ timeout: 40_000 });
  });
  await attempt(page, at("chat", "chat", "provider-error"), async () => {
    await chat(page, "/p/notes", "Break please [[error]]");
    await page.waitForTimeout(6000);
    await snap(page, "chat", "chat", "provider-error", "A turn whose provider call fails (the scripted LLM answers 500)");
  });
  await attempt(page, at("chat", "session-actions", "hover"), async () => {
    await page.goto("/p/notes/chat");
    // Sessions live in the sidebar: a row's "..." menu holds Rename / Delete.
    const row = page.getByTestId("chat-row").first();
    await row.hover();
    await page.waitForTimeout(400);
    await snap(page, "chat", "session-actions", "hover", "A sidebar chat row hovered: its '...' actions button", { widths: ["desktop"], viewportOnly: true, before: async (p) => { await p.getByTestId("chat-row").first().hover(); } });
    await row.getByRole("button", { name: /^Actions for/ }).click();
    await page.getByRole("menuitem", { name: "Rename" }).click();
    await page.waitForTimeout(300);
    await snap(page, "chat", "session-actions", "renaming", "Inline rename of a chat session", { widths: ["desktop"], viewportOnly: true });
    await page.keyboard.press("Escape");
  });
  await variants(page, "/p/notes/chat", { mode: M, area: "chat", name: "chat", what: "Chat (/p/notes/chat)" }, {
    match: keepShell([/^\/api\/projects\/notes$/, /^\/api\/projects$/]),
  });

  // Chats on billing too, so Usage has two projects.
  await attempt(page, at("seed", "billing-chat"), async () => {
    await chat(page, "/p/billing", "Summarise billing [[chart]]", "The chart above shows lines per symbol.");
  });

  await attempt(page, at("explorer", "node-detail", "rationale-generated"), async () => {
    await page.goto("/p/notes/explorer?node=notes.notes_main");
    await page.getByRole("tab", { name: "Rationale" }).click();
    await page.waitForTimeout(800);
    await snap(page, "explorer", "node-detail", "rationale-can-generate", "Rationale tab with an LLM configured: the Generate rationale button");
    const generate = page.getByRole("button", { name: "Generate rationale" });
    if (!(await generate.isVisible()) || !(await generate.isEnabled())) {
      gap(M, "explorer", "node-detail", "rationale-after-generate", "no enabled Generate rationale button on the Rationale tab");
      return;
    }
    await generate.click();
    await page.waitForTimeout(8000);
    await snap(page, "explorer", "node-detail", "rationale-after-generate", "Rationale tab after Generate rationale (the scripted LLM answers plain text, not the 5-section card)");
  });

  // Let the usage writer flush (it batches for up to a second).
  await page.waitForTimeout(2500);

  // ---- Usage & cost --------------------------------------------------------
  for (const tab of ["overview", "projects", "models", "calls", "budgets", "prices"]) {
    await attempt(page, at("usage", `usage-${tab}`), async () => {
      await page.goto(`/usage?tab=${tab}`);
      await page.waitForLoadState("networkidle").catch(() => undefined);
      await page.waitForTimeout(1200);
      await snap(page, "usage", `usage-${tab}`, "with-data", `Usage & cost, ${tab} tab (/usage?tab=${tab}) after ~15 scripted LLM calls on two projects`);
    });
  }
  await variants(page, "/usage", { mode: M, area: "usage", name: "usage-overview", what: "Usage & cost (/usage)" }, { states: ["loading", "error", "forbidden"] });

  // ---- budgets: a project hard stop, the org banner -----------------------
  await attempt(page, at("budgets", "chat", "budget-stopped"), async () => {
    await api(page.request, "PUT", `${env.baseUrl}/api/projects/notes/budget`, { monthly_usd: 0.01, hard_stop: true });
    await page.goto("/p/notes/chat");
    // A hard stop disables the sidebar's "New chat"; /chat already shows the notice.
    await expect(page.getByTestId("chat-budget-notice")).toBeVisible();
    await snap(page, "budgets", "chat", "budget-stopped", "Chat of notes with a hard-stopped $0.01 project budget: the composer is replaced by the budget notice");
    await page.goto("/p/notes");
    await page.waitForTimeout(1000);
    await snap(page, "budgets", "overview", "budget-stopped", "Project Overview with the hard-stopped project budget notice");
    await page.goto("/p/notes/explorer?node=notes.notes_main");
    await page.getByRole("tab", { name: "Rationale" }).click();
    await page.waitForTimeout(1000);
    await snap(page, "budgets", "explorer-rationale", "budget-stopped", "Explorer Rationale tab with the project's budget hard-stopped");
    await page.goto("/usage?tab=budgets");
    await page.waitForTimeout(1200);
    await snap(page, "budgets", "usage-budgets", "project-over", "Usage & cost, Budgets tab with notes' project budget exceeded");
  });
  await attempt(page, at("budgets", "projects", "org-banner"), async () => {
    await api(page.request, "DELETE", `${env.baseUrl}/api/projects/notes/budget`);
    const state = await api<{ usage?: { org?: { spent_usd: number } } }>(page.request, "GET", `${env.baseUrl}/api/portal/state`);
    const spent = state.usage?.org?.spent_usd ?? 1;
    await api(page.request, "PUT", `${env.baseUrl}/api/budgets/org`, { monthly_usd: Math.max(0.05, Math.round((spent / 0.8) * 100) / 100), hard_stop: false });
    await page.goto("/");
    await page.waitForTimeout(1500);
    await snap(page, "budgets", "projects", "org-banner-75", "Projects page with the org budget at ~80%: the owner's org budget banner");
    await page.goto("/usage");
    await page.waitForTimeout(1500);
    await snap(page, "budgets", "usage-overview", "org-banner-75", "Usage & cost overview with the org budget banner and gauge");
    await api(page.request, "PUT", `${env.baseUrl}/api/budgets/org`, { monthly_usd: Math.max(0.01, Math.round(spent * 0.9 * 100) / 100), hard_stop: true });
    await page.goto("/");
    await page.waitForTimeout(1500);
    await snap(page, "budgets", "projects", "org-banner-100", "Projects page with the org budget exceeded and hard-stopped");
    await page.goto("/p/billing/chat");
    await page.waitForTimeout(1200);
    await snap(page, "budgets", "chat", "org-stopped", "Chat with the organization's budget hard-stopped");
    await api(page.request, "DELETE", `${env.baseUrl}/api/budgets/org`);
  });

  // ---- settings ------------------------------------------------------------
  await attempt(page, at("settings", "project-settings"), async () => {
    await page.goto("/p/notes/settings");
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await page.waitForTimeout(1200);
    await snap(page, "settings", "project-settings", "default", "Project settings of notes (/p/notes/settings): General, models/keys/hooks, Usage, Agents, Danger zone");
    await page.getByRole("button", { name: "Remove project" }).click();
    await page.waitForTimeout(600);
    await snap(page, "settings", "remove-project-dialog", "open", "Danger zone: the Remove project dialog", { viewportOnly: true });
    await page.keyboard.press("Escape");
  });
  await variants(page, "/p/notes/settings", { mode: M, area: "settings", name: "project-settings", what: "Project settings (/p/notes/settings)" }, {
    match: keepShell([/^\/api\/projects\/notes$/, /^\/api\/projects$/]),
  });
  await attempt(page, at("settings", "project-settings", "docs-site"), async () => {
    await page.goto("/p/docs-site/settings");
    await page.waitForTimeout(1500);
    await snap(page, "settings", "project-settings", "not-initialized", "Project settings of docs-site (not initialized)");
  });
  await attempt(page, at("settings", "global-settings", "with-keys"), async () => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await page.waitForTimeout(1000);
    await snap(page, "settings", "global-settings", "with-keys", "Global Settings after an OpenAI key, a custom base URL and the chat model were set");
  });
  await variants(page, "/settings", { mode: M, area: "settings", name: "global-settings", what: "Global Settings (/settings)" });

  // ---- edges ---------------------------------------------------------------
  await attempt(page, at("edge", "account"), async () => {
    await page.goto("/account");
    await page.waitForTimeout(1500);
    await snap(page, "edge", "account", "local-mode", "/account in local mode (a production-only page)");
  });
  await attempt(page, at("edge", "not-found"), async () => {
    await page.goto("/no-such-page");
    await page.waitForTimeout(1200);
    await snap(page, "edge", "not-found", "unknown-route", "An unknown route (/no-such-page)");
    await page.goto("/p/no-such-project");
    await page.waitForTimeout(2000);
    await snap(page, "edge", "not-found", "unknown-project", "An unknown project slug (/p/no-such-project)");
    await page.goto("/members");
    await page.waitForTimeout(1200);
    await snap(page, "edge", "not-found", "members-local", "/members in local mode (production only)");
  });
  await attempt(page, at("edge", "api-down"), async () => {
    const restore = await failRequests500All(page);
    try {
      await page.goto("/");
      await page.waitForTimeout(4000);
      await snap(page, "edge", "api-down", "portal-state-500", "Every API call answered 500, including /api/portal/state (root error boundary)");
    } finally {
      await restore();
    }
  });

  // ---- the project list once more (web finished) ---------------------------
  await attempt(page, at("projects", "projects", "settled"), async () => {
    await page.goto("/");
    await page.waitForTimeout(1500);
    await snap(page, "projects", "projects", "settled", "Projects page at the end of the local run (no scan running)");
  });
});

async function failRequests500All(page: Page): Promise<() => Promise<void>> {
  const handler = (route: import("@playwright/test").Route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Internal Server Error" }) });
  await page.route("**/api/**", handler);
  return () => page.unroute("**/api/**", handler);
}

// Things this spec knows it cannot reach in local mode.
test.afterAll(() => {
  gap(M, "members/audit/org", "members, audit, org settings", "all", "production only: local mode has one implicit user and no organization pages (captured under production/)");
});
