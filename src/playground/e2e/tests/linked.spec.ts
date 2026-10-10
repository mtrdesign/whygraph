import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { env } from "../env";
import { base, createOrg, githubSignIn, importRepo, orgUrl, signedIn } from "../lib/production";

// A project linked to a platform (M2e, plan section 4.14): imported on the
// production portal, linked from the local one through the platform's own
// `/connect` consent page, then asked for evidence over the local portal's MCP
// endpoint while the checkout carries an uncommitted edit and an unpushed
// commit. Finally the admin revokes the connection and the checkout is removed
// from this machine.
//
// It drives both portals: `baseURL` is the local one, the platform is opened by
// absolute URL (its own host carries the org). The `production` project has
// already claimed the instance and signed Ben and Cy in once on the fake GitHub.

const ORG = "orbit";
const REPO = "ben/notes";
const SLUG = "notes";
const MACHINE = "e2e-laptop.local";

function git(repo: string, ...args: string[]): string {
  return execFileSync("git", ["-C", repo, ...args], { stdio: "pipe", encoding: "utf8" });
}

/**
 * A checkout of the fake's bare fixture repository under the shared folder.
 *
 * Cloned from the bare repo on disk, because the fake serves git over HTTP only
 * to a live installation token - and then `origin` is pointed at the URL the
 * platform clones from, as a developer's checkout would have it.
 */
function cloneFixture(): string {
  const repo = path.join(env.shared, SLUG);
  fs.rmSync(repo, { recursive: true, force: true });
  execFileSync("git", ["clone", "-q", path.join(env.githubRepos, `${REPO}.git`), repo], {
    stdio: "pipe",
  });
  git(repo, "config", "user.email", "cy@example.com");
  git(repo, "config", "user.name", "Cy");
  git(repo, "config", "commit.gpgsign", "false");
  git(repo, "remote", "set-url", "origin", `${env.githubUrl}/${REPO}.git`);
  return repo;
}

/** The shared-folder listing, read in the page (its own cookies and headers). */
async function sharedRepos(page: Page): Promise<string[]> {
  return page.evaluate(async () => {
    const r = await fetch("/api/portal/repos", { headers: { "X-WhyGraph-Client": "1" } });
    if (!r.ok) throw new Error(`repos answered ${r.status}`);
    return ((await r.json()) as { repos: { path: string }[] }).repos.map((x) => x.path);
  });
}

interface EvidenceItem {
  commit: { sha: string; subject: string };
  source: string;
  push_status?: string;
}
interface EvidenceAnswer {
  evidence: EvidenceItem[];
  platform: { status: string; url: string };
}
interface ToolResult {
  isError?: boolean;
  structuredContent?: { result?: unknown } & Record<string, unknown>;
  content?: { text: string }[];
}

/** The JSON-RPC message of a one-event SSE body (what the MCP endpoint answers). */
function sseJson(text: string): { result?: ToolResult; error?: unknown } {
  for (const line of text.split("\n")) {
    if (line.startsWith("data: ")) return JSON.parse(line.slice(6));
  }
  return JSON.parse(text);
}

/**
 * `whygraph_evidence_for` over the local portal's own MCP endpoint, exactly as a
 * coding agent calls it: one stateless Streamable HTTP POST, no session.
 */
async function evidenceFor(
  request: APIRequestContext,
  lineEnd: number,
): Promise<EvidenceAnswer> {
  const response = await request.post(`${env.baseUrl}/mcp/${SLUG}`, {
    headers: { Accept: "application/json, text/event-stream", "Content-Type": "application/json" },
    data: {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: "whygraph_evidence_for",
        arguments: { path: "README.md", line_start: 1, line_end: lineEnd },
      },
    },
  });
  const body = await response.text();
  expect(response.status(), body).toBe(200);
  const message = sseJson(body);
  expect(message.error, body).toBeUndefined();
  const result = message.result;
  expect(result, body).toBeDefined();
  expect(result!.isError, body).toBeFalsy();
  const structured = result!.structuredContent;
  const payload =
    structured && Object.keys(structured).join() !== "result"
      ? structured
      : (structured?.result ?? JSON.parse(result!.content![0].text));
  return payload as EvidenceAnswer;
}

/** The evidence item of one commit, when the answer carries it. */
const itemFor = (answer: EvidenceAnswer, sha: string) =>
  answer.evidence.find((e) => e.commit.sha === sha);

test("a linked project: connect, evidence over MCP, revocation, removal", async ({
  page,
  browser,
  request,
}) => {
  // Two portals, two sign-ins, two scans and a git clone: far longer than the
  // suite's default per-test budget.
  test.setTimeout(300_000);

  // --- the platform: Ben imports the repository and adds Cy as a member -----
  const benContext = await browser.newContext({ baseURL: env.prodUrl });
  const ben = await benContext.newPage();
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

  // --- this machine: a checkout of the same repository in a shared folder ---
  const repo = cloneFixture();
  const pushedSha = git(repo, "rev-parse", "HEAD").trim();
  // The link's candidates are computed once, from the discovery cache (60 s), so
  // wait until the portal has actually seen the new checkout.
  await page.goto("/projects/new");
  await expect.poll(() => sharedRepos(page), { timeout: 70_000 }).toContain(repo);

  // --- the connect round trip, with the sign-in on the platform ------------
  await page.goto("/projects/new?source=platform");
  await page.getByLabel("Platform address").fill(env.prodUrl);
  await page.getByLabel("Machine name").fill(MACHINE);
  await page.getByRole("button", { name: "Connect" }).click();

  // Cy has no session on the platform in this browser: the base host asks him to
  // sign in and hands him back to the consent page with the request intact.
  await expect(page).toHaveURL(new RegExp(`^${base.origin}/signin\\?next=`));
  expect(new URL(page.url()).searchParams.get("next")).toContain("/connect?");
  await githubSignIn(page, "cy");
  await expect(page).toHaveURL(new RegExp(`^${base.origin}/connect`));
  await expect(page.getByTestId("connect-client")).toHaveText(MACHINE);
  await page.getByLabel("Project").selectOption({ label: `Orbit / ${SLUG}` });
  await page.getByRole("button", { name: "Allow" }).click();

  // Back on this machine: the callback posted the code once and the wizard went
  // on to the checkout picker. The fake GitHub serves git over `http`, and an
  // origin identity is only read off an `https` or `ssh` remote, so the clone
  // URL matches nothing: our checkout is offered under *Other repositories*
  // and accepted because it holds the commit the platform last scanned
  // (plan section 0.1 #12).
  await expect(page).toHaveURL(new RegExp(`^${env.baseUrl}/projects/new\\?.*link=`));
  const picker = page.getByTestId("platform-picker");
  await expect(picker).toContainText(`${ORG}/${SLUG}`);
  await expect(picker.getByTestId("no-candidates")).toBeVisible();
  const others = picker.getByRole("radiogroup", { name: "Other repositories" });
  await others
    .locator("label")
    .filter({ has: page.getByText(repo, { exact: true }) })
    .getByRole("radio")
    .check();
  await page.getByRole("button", { name: "Link this checkout" }).click();

  // A linked project's wizard is Source -> Set up (the platform owns the config).
  // With no agent ticked it warns; Set up queues the first scan, which indexes
  // the code structure only, and the done panel follows it.
  await expect(page).toHaveURL(new RegExp(`/p/${SLUG}/init\\?step=setup`));
  await expect(page.getByTestId("no-agent-warning")).toBeVisible();
  await page.getByRole("checkbox", { name: /Claude Code/ }).check();
  await expect(page.getByTestId("no-agent-warning")).toHaveCount(0);
  await expect(page.getByTestId("init-preview")).toBeVisible();
  await page.getByRole("button", { name: "Finish", exact: true }).click();
  await expect(page.getByTestId("init-done")).toContainText("Project linked");
  await expect(page.getByText("First scan complete")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("connect-agent")).toBeVisible();
  await page.getByRole("button", { name: "Open project", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${SLUG}$`));
  await expect(page.getByTestId("link-notice")).toHaveAttribute("data-status", "ok");
  await expect(page.getByTestId("link-notice")).toContainText(`Linked to ${ORG}/${SLUG}`);

  // --- the agent's question, with local work in the way --------------------
  const readme = path.join(repo, "README.md");
  fs.appendFileSync(readme, "Committed here, never pushed.\n");
  git(repo, "commit", "-qam", "A local commit");
  const unpushedSha = git(repo, "rev-parse", "HEAD").trim();
  fs.appendFileSync(readme, "Not committed at all.\n");

  const answer = await evidenceFor(request, 3);
  expect(answer.platform.status).toBe("ok");
  // Line 1 is the pushed commit the platform scanned: its own row, no label.
  const pushed = itemFor(answer, pushedSha);
  expect(pushed, JSON.stringify(answer.evidence)).toBeDefined();
  expect(pushed!.push_status).toBeUndefined();
  expect(pushed!.source).not.toBe("local");
  // Line 2 is committed but unpushed, line 3 is not committed: both stay here.
  expect(itemFor(answer, unpushedSha)?.push_status).toBe("not_pushed");
  const uncommitted = answer.evidence.filter((e) => e.push_status === "uncommitted");
  expect(uncommitted).toHaveLength(1);
  expect(uncommitted[0].source).toBe("local");

  // --- the admin revokes this machine's connection -------------------------
  await ben.goto(`${orgUrl(ORG)}/p/${SLUG}/settings`);
  const connections = ben.getByTestId("project-connections");
  const row = connections.getByTestId("connection-row").filter({ hasText: MACHINE });
  await expect(row).toContainText("@cy");
  await row.getByRole("button", { name: "Revoke" }).click();
  await expect(connections).toContainText("No local portal is connected to this project.");

  // The next question gets no history from the platform, and every hunk is
  // labelled from this checkout's refs - the pushed one as awaiting a scan.
  const revoked = await evidenceFor(request, 3);
  expect(revoked.platform.status).toBe("revoked");
  expect(revoked.evidence).toHaveLength(3);
  expect(revoked.evidence.filter((e) => e.source === "local")).toHaveLength(3);
  expect(itemFor(revoked, pushedSha)?.push_status).toBe("pending_scan");

  // That answer refreshed the link's status, so the card says so.
  await page.goto("/");
  const notice = page.getByTestId("link-notice");
  await expect(notice).toHaveAttribute("data-status", "revoked");
  await expect(notice).toContainText("Access revoked (Revoked by an admin)");

  // --- remove from this machine -------------------------------------------
  await page.goto(`/p/${SLUG}/settings`);
  await page.getByRole("button", { name: "Remove from this machine" }).click();
  const dialog = page.getByTestId("remove-dialog");
  await dialog.getByRole("button", { name: "Remove from this machine" }).click();
  await expect(page.getByTestId("remove-done")).toBeVisible();
  // The token was already revoked there (`token_revoke_result: already_revoked`),
  // so nothing is left to revoke and the dialog warns about nothing (BUG-7).
  await expect(page.getByTestId("revoke-failed")).toHaveCount(0);
  await page.getByRole("button", { name: "Back to projects" }).click();
  await expect(page.getByTestId(`project-${SLUG}`)).toHaveCount(0);
  // The checkout itself is untouched; only the portal's markers are gone.
  expect(fs.existsSync(readme)).toBe(true);
  expect(fs.existsSync(path.join(repo, ".whygraph", "portal.json"))).toBe(false);

  await benContext.close();
});
