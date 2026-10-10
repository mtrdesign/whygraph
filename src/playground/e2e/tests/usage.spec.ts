import { expect, test } from "@playwright/test";
import { env } from "../env";
import { themeRepos } from "../lib/fixtures";
import { themeOf } from "../lib/ui";
import { call, chatOnce, configureStubLlm, reloadUntilVisible, STUB_MODEL } from "../lib/usage";

// Usage & cost (M2f-2) in local mode: a chat message is one ledger row, priced
// through an org price override on the fake LLM (tests/llm_fake.py), and a hard
// stop on the theme's project replaces the chat composer. Both colour passes run
// this on their own project, so the budget is a project's, never the org's.
test("a chat message shows on Usage & cost, then a hard-stopped project budget closes the composer", async ({
  page,
}, testInfo) => {
  const { notes } = themeRepos(themeOf(testInfo));
  await page.goto("/");
  await configureStubLlm(page.request, env.baseUrl);

  await chatOnce(page, `/p/${notes.slug}`, "hello usage");

  // The ledger writer batches for up to a second: reload until the row shows.
  await page.goto(`/usage?tab=calls&project=${notes.slug}`);
  const rows = page.getByTestId("calls-table").locator("tbody tr");
  await reloadUntilVisible(page, rows, page.getByText("No calls match"));
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText(STUB_MODEL);
  await expect(rows.first()).toContainText("20k / 4k");
  await expect(rows.first()).toContainText(/\$0\.\d\d/);
  await expect(rows.first()).not.toContainText("unpriced");

  // A budget far below that spend, with the hard stop, on this project only -
  // set from the settings' Usage section, whose form saves on its own (R3).
  await page.goto(`/p/${notes.slug}/settings?section=budgets`);
  const budget = page.getByTestId("project-budget");
  await budget.getByLabel("Monthly budget (USD)").fill("0.01");
  await budget.getByRole("switch").click();
  await budget.getByRole("button", { name: "Save", exact: true }).click();
  await expect(budget.getByText("Saved")).toBeVisible();
  await page.goto(`/p/${notes.slug}/chat`);
  await expect(page.getByTestId("chat-budget-notice")).toBeVisible();
  // The Chats section's "+" is disabled with the reason (USE-4).
  await expect(page.getByTestId("chats-section").getByRole("button", { name: "New chat" })).toBeDisabled();
  await expect(page.getByPlaceholder("Ask about this repository…")).toHaveCount(0);

  // Any other page of the project carries the stop once, as a banner (USE-1, USE-3): the
  // Stopped pill, when it resets, and no dismiss button - Chat itself shows no banner.
  await expect(page.getByTestId("budget-notice")).toHaveCount(0);
  await page.goto(`/p/${notes.slug}`);
  const stop = page.getByTestId("budget-notice");
  await expect(stop).toContainText("Stopped");
  // The reset day in the viewer's locale: "Nov 1" or "1 Nov" (CN-3).
  await expect(stop).toContainText(/Resets (\d+ \w{3}|\w{3} \d+)\./);
  await expect(stop.getByRole("button", { name: "Dismiss for this month" })).toHaveCount(0);
  // Local mode's one user raises it themself (USE-2).
  await expect(stop.getByRole("link", { name: "Raise it in Budgets" })).toBeVisible();

  // Leave the project usable for any spec that follows.
  await call(page.request, "DELETE", `${env.baseUrl}/api/projects/${notes.slug}/budget`);
});
