import { expect, test } from "@playwright/test";
import { env } from "../env";
import { base, createOrg, githubSignIn, importRepo, orgUrl, signedIn } from "../lib/production";
import { call, chatOnce, configureStubLlm, reloadUntilVisible, STUB_MODEL } from "../lib/usage";

// Usage & cost (M2f-2) in production mode, on its own organization: a contributor
// chats, the owner sees them on Members and in the drill-down, the contributor
// sees only My usage, and a hard-stopped member budget blocks that member while
// another still chats. One run (the `production-usage` project), so the member
// budget never collides across colour passes.
interface MemberRow {
  uid: string;
  github_login: string;
}

test("member usage: the owner sees who spent, a member sees only their own, a member budget stops one member", async ({
  browser,
}) => {
  test.setTimeout(300_000);
  const org = orgUrl("meter");
  const as = async (login: string) => {
    const context = await browser.newContext({ baseURL: env.prodUrl });
    const page = await context.newPage();
    await githubSignIn(page, login);
    await signedIn(page);
    return { context, page };
  };

  // Ben creates the org, imports a project and points the org at the fake LLM.
  const { context: benContext, page: ben } = await as("ben");
  await ben.goto(`${base.origin}/orgs/new`);
  await createOrg(ben, "Meter", "meter");
  await importRepo(ben, "meter", "ben/demo");
  await configureStubLlm(ben.request, org);

  // cy and dee join as members with a Contributor grant on demo (the default
  // role cannot chat).
  for (const login of ["cy", "dee"]) {
    await ben.goto(`${org}/members`);
    await ben.getByLabel("GitHub username").fill(login);
    await ben.getByLabel("Access to demo").selectOption("contributor");
    await ben.getByRole("button", { name: "Invite", exact: true }).click();
    await expect(ben.getByText(`@${login}`).first()).toBeVisible();
  }
  const { context: cyContext, page: cy } = await as("cy");
  const { context: deeContext, page: dee } = await as("dee");

  const members = await call<MemberRow[]>(ben.request, "GET", `${org}/api/org/members`);
  const uid = (login: string) => members.find((m) => m.github_login === login)!.uid;

  // cy chats once.
  await chatOnce(cy, `${org}/p/demo`, "hello from cy");

  // The owner finds cy on the Members tab (the ledger writer batches for up to
  // a second, so poll with reloads), then in the drill-down.
  await ben.goto(`${org}/usage?tab=members`);
  const row = ben.getByTestId(`breakdown-row-${uid("cy")}`);
  await reloadUntilVisible(ben, row, ben.getByTestId("breakdown-member").or(ben.getByText("No LLM calls in this range.")));
  await expect(row).toContainText(/\$0\.\d\d/);
  await row.getByRole("link").click();
  await expect(ben).toHaveURL(new RegExp(`/usage/members/${uid("cy")}`));
  await expect(ben.getByTestId("member-usage-page")).toContainText("@cy");
  await expect(ben.getByTestId("member-tile-cost")).toContainText(/\$0\.\d\d/);
  await expect(ben.getByTestId("expensive-calls")).toContainText(STUB_MODEL);

  // cy sees only My usage: /usage sends a member there, and the drill-down of
  // another member is closed.
  await cy.goto(`${org}/usage`);
  await expect(cy).toHaveURL(new RegExp(`${org}/usage/me`));
  await expect(cy.getByTestId("member-usage-page")).toBeVisible();
  await expect(cy.getByTestId("member-tile-cost")).toContainText(/\$0\.\d\d/);
  const others = await cy.request.get(`${org}/api/usage?member=${uid("ben")}`, { headers: { "X-WhyGraph-Client": "1" } });
  expect(others.status()).toBe(403);

  // A hard-stopped member budget on cy: his composer is replaced, dee's is not.
  await call(ben.request, "PUT", `${org}/api/budgets/members/${uid("cy")}`, { monthly_usd: 0.01, hard_stop: true });
  await cy.goto(`${org}/p/demo/chat`);
  await cy.getByRole("button", { name: "New chat" }).click();
  await expect(cy.getByTestId("chat-budget-notice")).toBeVisible();
  await chatOnce(dee, `${org}/p/demo`, "hello from dee");

  await benContext.close();
  await cyContext.close();
  await deeContext.close();
});
