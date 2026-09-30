import path from "node:path";
import { expect, test } from "@playwright/test";
import { env } from "../env";
import { expectScheme, themeOf } from "../lib/ui";

// Screen 3's alert: the portal runs in Docker and only sees the folders it was
// started with. A real git repo outside them must be refused with the command
// that shares it, and nothing may be registered.
test("a repo outside the shared folders shows the not-shared alert and cannot be added", async ({
  page,
  request,
}, testInfo) => {
  await page.goto("/projects/new");
  await expectScheme(page, themeOf(testInfo));

  await page.getByLabel("Or enter a path").fill(env.outside);
  await page.getByRole("button", { name: "Check" }).click();

  const alert = page.getByTestId("not-shared-alert");
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("This folder isn't shared with the portal");
  // The command comes from the backend, already quoted, and shares the repo's parent folder.
  await expect(alert.locator("code")).toContainText("whygraph up --add-folder");
  await expect(alert.locator("code")).toContainText(path.dirname(env.outside));
  await expect(alert.getByRole("button", { name: "Copy" })).toBeVisible();
  await expect(alert.getByRole("button", { name: "Check again" })).toBeEnabled();

  // Nothing offers to add it.
  await expect(page.getByText("Ready to add")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add project" })).toBeDisabled();

  // "Check again" re-runs the check (the folder is still not shared).
  await alert.getByRole("button", { name: "Check again" }).click();
  await expect(alert).toBeVisible();

  // And the server agrees: the same path is refused when posted straight to the API.
  const res = await request.post("/api/projects", {
    headers: { "X-WhyGraph-Client": "1" },
    data: { source: "local", path: env.outside },
  });
  expect(res.ok()).toBe(false);
  const projects = await (await request.get("/api/projects", { headers: { "X-WhyGraph-Client": "1" } })).json();
  expect(projects.projects.map((p: { root: string }) => p.root)).not.toContain(env.outside);
});
