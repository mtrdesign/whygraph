import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { env } from "../env";
import { themeRepos } from "../lib/fixtures";
import { themeOf } from "../lib/ui";

// Scan runs on an already-initialized project (main-flow.spec.ts sets it up).
// The fake scanner reads control/delay and control/fail on every run.
const flag = (name: string) => path.join(env.control, name);

test.afterEach(() => {
  fs.rmSync(flag("fail"), { force: true });
  fs.rmSync(flag("delay"), { force: true });
});

test("two clicks on Scan now during a run coalesce onto one follow-up run", async ({ page }, testInfo) => {
  const { notes } = themeRepos(themeOf(testInfo));
  fs.writeFileSync(flag("delay"), "0.4"); // ~5 s per phase-1 pass: room to click

  const posts: number[] = [];
  page.on("response", async (r) => {
    if (r.request().method() === "POST" && /\/api\/projects\/[^/]+\/scans$/.test(r.url())) {
      posts.push((await r.json()).run_id);
    }
  });

  await page.goto(`/p/${notes.slug}`);
  await page.getByRole("button", { name: "Scan now" }).first().click();
  await page.waitForURL(`**/p/${notes.slug}/scans/*`);
  const running = Number(new URL(page.url()).pathname.split("/").pop());

  // Live progress: phase 1 is running and its bar moves.
  await expect(page.getByTestId("phase-1")).toHaveAttribute("data-status", "running");
  const bar = page.getByTestId("task-git").getByRole("progressbar");
  await expect(bar).toBeVisible();
  const before = Number(await bar.getAttribute("aria-valuenow"));
  await expect.poll(async () => Number(await bar.getAttribute("aria-valuenow"))).toBeGreaterThan(before);

  // Two more clicks while it runs: one pending follow-up, the same id twice.
  posts.length = 0;
  const again = page.getByRole("button", { name: "Scan now" });
  await again.click();
  await expect(page.getByTestId("followup")).toBeVisible();
  await again.click();
  await expect.poll(() => posts.length).toBe(2);
  expect(new Set(posts).size).toBe(1);
  expect(posts[0]).not.toBe(running);
  await expect(page.getByTestId("followup")).toHaveCount(1);

  // The running run finishes, then the follow-up does; history lists both.
  await expect(page.getByTestId("run-result")).toContainText("Finished in", { timeout: 40_000 });
  fs.rmSync(flag("delay"), { force: true });
  await page.goto(`/p/${notes.slug}/scans/${posts[0]}`);
  await expect(page.getByTestId("run-result")).toContainText("Finished in", { timeout: 40_000 });
  await page.goto(`/p/${notes.slug}/scans`);
  await expect(page.getByTestId(`run-${running}`)).toContainText("Succeeded");
  await expect(page.getByTestId(`run-${posts[0]}`)).toContainText("Succeeded");
});

test("a failing scan shows the error and the log, and the project card says so", async ({ page }, testInfo) => {
  const { notes } = themeRepos(themeOf(testInfo));
  fs.writeFileSync(flag("fail"), "1");

  await page.goto(`/p/${notes.slug}`);
  await page.getByRole("button", { name: "Scan now" }).first().click();
  await page.waitForURL(`**/p/${notes.slug}/scans/*`);

  await expect(page.getByTestId("run-result")).toContainText("The scan failed", { timeout: 30_000 });
  // A failed run opens its log on its own; open it only if it is still closed.
  const toggle = page.getByTestId("run-log").getByRole("button", { name: /Log/ });
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await expect(page.getByTestId("run-log").locator("pre")).toContainText("simulated crawler error");

  await page.goto("/");
  await expect(page.getByTestId(`project-${notes.slug}`)).toContainText("Scan failed");

  // A good scan afterwards clears the badge.
  fs.rmSync(flag("fail"), { force: true });
  await page.goto(`/p/${notes.slug}`);
  await page.getByRole("button", { name: "Scan now" }).first().click();
  await expect(page.getByTestId("run-result")).toContainText("Finished in", { timeout: 40_000 });
  await page.goto("/");
  await expect(page.getByTestId(`project-${notes.slug}`)).not.toContainText("Scan failed");
});

test("Cancel stops a running scan; the run and the history say it was cancelled", async ({ page }, testInfo) => {
  const { notes } = themeRepos(themeOf(testInfo));
  fs.writeFileSync(flag("delay"), "0.4");

  await page.goto(`/p/${notes.slug}`);
  await page.getByRole("button", { name: "Scan now" }).first().click();
  await page.waitForURL(`**/p/${notes.slug}/scans/*`);
  const running = Number(new URL(page.url()).pathname.split("/").pop());
  await expect(page.getByTestId("phase-1")).toHaveAttribute("data-status", "running");

  await page.getByTestId("cancel-run").click();
  const dialog = page.getByTestId("cancel-run-dialog");
  await expect(dialog).toContainText(`Cancel scan #${running}?`);
  await dialog.getByRole("button", { name: "Cancel scan" }).click();

  await expect(page.getByTestId("run-result")).toContainText("You cancelled this run", { timeout: 20_000 });
  await expect(page.getByTestId("cancel-run")).toHaveCount(0);
  await page.goto(`/p/${notes.slug}/scans`);
  await expect(page.getByTestId(`run-${running}`)).toContainText("Cancelled by you");
});
