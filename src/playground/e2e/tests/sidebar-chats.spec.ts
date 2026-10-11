import { expect, test } from "@playwright/test";
import { env } from "../env";
import { themeRepos } from "../lib/fixtures";
import { expectSkipLink, tabUntil, themeOf } from "../lib/ui";
import { configureStubLlm, STUB_REPLY } from "../lib/usage";

// The sidebar's Chats section (M2f-3): the "+" opens an empty thread and creates a
// session only with the first message, a row renames and deletes from its "..."
// menu, and a keyboard user reaches that menu. It works on the theme's second
// project so the first one's usage ledger (usage.spec.ts) stays untouched.
test("Chats: '+' creates a session on the first message, rename and delete from the menu, keyboard", async ({
  page,
}, testInfo) => {
  const { billing } = themeRepos(themeOf(testInfo));
  await page.goto("/");
  await configureStubLlm(page.request, env.baseUrl);

  await page.goto(`/p/${billing.slug}`);
  const chats = page.getByTestId("chats-section");
  const rows = chats.getByTestId("chat-row");
  await expect(chats.getByText("No chats yet")).toBeVisible();

  // The skip link is the first stop and moves focus to the page.
  await expectSkipLink(page);

  // "+" opens an empty thread: still no row until a message is sent.
  await chats.getByRole("button", { name: "New chat" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${billing.slug}/chat$`));
  await expect(page.getByPlaceholder("Ask about this repository…")).toBeVisible();
  await expect(rows).toHaveCount(0);
  await expect(chats.getByText("No chats yet")).toBeVisible();

  await page.getByPlaceholder("Ask about this repository…").fill("hello from the sidebar spec");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByText(STUB_REPLY).first()).toBeVisible();
  await expect(page).toHaveURL(/\/chat\/\d+$/);
  await expect(rows).toHaveCount(1);

  // Keyboard: Tab from the top of the page reaches the row's "..." menu, and Enter opens it.
  await page.goto(page.url());
  const menu = rows.first().getByRole("button", { name: /^Actions for / });
  await tabUntil(page, menu);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("menuitem", { name: "Rename" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Delete" })).toBeVisible();
  await page.keyboard.press("Escape");

  // Rename from the menu: Enter saves, the menu's label follows, a reload keeps it.
  await menu.click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  const field = chats.getByLabel("Chat title");
  await expect(field).toBeFocused();
  await field.fill("Renamed by the e2e");
  await field.press("Enter");
  await expect(rows.first()).toContainText("Renamed by the e2e");
  await expect(chats.getByRole("button", { name: "Actions for Renamed by the e2e" })).toBeVisible();
  await page.reload();
  await expect(chats.getByRole("button", { name: "Actions for Renamed by the e2e" })).toBeVisible();

  // Delete from the menu: confirm, and the row is gone.
  await chats.getByRole("button", { name: "Actions for Renamed by the e2e" }).click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Renamed by the e2e");
  await dialog.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(rows).toHaveCount(0);
  await expect(chats.getByText("No chats yet")).toBeVisible();
});
