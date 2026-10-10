import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { themeRepos } from "../lib/fixtures";
import {
  addLocalProject,
  setUpProject,
  expectScheme,
  firstScan,
  openMainSymbol,
  sidebarLink,
  themeOf,
} from "../lib/ui";

// The main flow of a new project: add a local repo, set it up (which queues the
// first scan), follow that scan on Configure (the fake scanner), and open the
// Explorer on what it indexed.
// Steps share one portal and build on each other, so they run in order.
test.describe.configure({ mode: "serial" });

test.describe("main flow", () => {
  test("add a repo from the shared-folder list and set it up", async ({ page }, testInfo) => {
    const theme = themeOf(testInfo);
    const { notes } = themeRepos(theme);

    await page.goto("/");
    await expectScheme(page, theme);

    await addLocalProject(page, notes, "list");
    await expect(page.getByRole("heading", { name: `${notes.name}: Set up` })).toBeVisible();
    await setUpProject(page, notes);
    // Configure follows the queued first scan with one bar and its steps.
    await expect(page.getByRole("heading", { name: `${notes.name}: Configure` })).toBeVisible();
    await expect(page.getByTestId("scan-progress")).toBeVisible();
    await expect(page.getByRole("link", { name: "More settings (chat model, hooks, limits)" })).toBeVisible();

    // Set up wrote the repo-side wiring: the marker, the MCP entry, hooks.
    const marker = JSON.parse(fs.readFileSync(path.join(notes.path, ".whygraph", "portal.json"), "utf8"));
    expect(marker.slug).toBe(notes.slug);
    const mcp = JSON.parse(fs.readFileSync(path.join(notes.path, ".mcp.json"), "utf8"));
    expect(mcp.mcpServers.whygraph.url).toContain(`/mcp/${notes.slug}`);
    expect(fs.readFileSync(path.join(notes.path, ".git", "hooks", "post-commit"), "utf8")).toContain("whygraph managed");
  });

  test("Configure follows the first scan after a reload and ends on the estimate", async ({ page }, testInfo) => {
    const { notes } = themeRepos(themeOf(testInfo));
    await page.goto(`/p/${notes.slug}/init?step=scan`);
    await firstScan(page, notes);

    // The project overview knows it was scanned: its health, the tiles, coverage
    // history starting with the next scan, and (no agent call yet) the setup card.
    await expect(page.getByTestId("health-panel")).toBeVisible();
    await expect(page.getByTestId("stat-described")).toContainText("Commits described");
    await expect(page.getByTestId("coverage-card")).toBeVisible();
    await expect(page.getByTestId("connect-agent")).toBeVisible();
    await expect(page.getByTestId("mcp-url")).toContainText(`/mcp/${notes.slug}`);
    await page.goto("/");
    await expect(page.getByTestId(`project-${notes.slug}`)).toContainText("Scanned");
  });

  test("the Explorer loads the scanned project", async ({ page }, testInfo) => {
    const { notes } = themeRepos(themeOf(testInfo));
    await page.goto(`/p/${notes.slug}`);
    await sidebarLink(page, "Explorer").click();
    await expect(page).toHaveURL(new RegExp(`/p/${notes.slug}/explorer`));

    // The containment tree reads what the scan indexed: src/ -> the file -> its functions.
    await openMainSymbol(page, notes);
    await expect(page.getByText(notes.helperSymbol).first()).toBeVisible();
  });
});
