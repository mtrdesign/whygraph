import fs from "node:fs";
import path from "node:path";
import { expect, type APIRequestContext, type Page } from "@playwright/test";
import { env } from "../../env";

// Seeding helpers for the audit: the scripted LLM, chats, budgets, scan control.

/** Where global-setup starts `llm_script.py`. */
export const AUDIT_LLM_PORT = Number(process.env.AUDIT_LLM_PORT ?? 18769);
export const AUDIT_MODEL = "e2e-audit-model";
export const AUDIT_LLM_URL = `http://127.0.0.1:${AUDIT_LLM_PORT}/v1`;

/** The local-mode fixture repos (folder name = slug), created by global-setup. */
export const LOCAL_REPOS = ["notes", "billing", "web", "docs-site", "legacy"] as const;

const HEADERS = { "X-WhyGraph-Client": "1" };

/** One JSON call to a portal's API; fails on a non-2xx answer. */
export async function api<T = unknown>(
  request: APIRequestContext,
  method: "GET" | "PUT" | "POST" | "PATCH" | "DELETE",
  url: string,
  data?: unknown,
): Promise<T> {
  const res = await request.fetch(url, { method, headers: HEADERS, data });
  expect(res.status(), `${method} ${url}: ${await res.text()}`).toBeLessThan(300);
  return (res.status() === 204 ? undefined : await res.json()) as T;
}

/** Point `origin`'s organization at the scripted LLM and price its model. */
export async function configureAuditLlm(request: APIRequestContext, origin: string): Promise<void> {
  await api(request, "PUT", `${origin}/api/portal/defaults`, {
    config: { llm: { openai: { base_url: AUDIT_LLM_URL } }, chat: { model: `openai/${AUDIT_MODEL}` } },
    secrets: { llm: { openai: "sk-audit-stub" } },
  });
  await api(request, "PUT", `${origin}/api/prices`, {
    provider: "openai",
    model: AUDIT_MODEL,
    input_per_mtok: 3,
    output_per_mtok: 15,
  });
}

export const COMPOSER = "Ask about this repository…";

/** Start a new chat on `projectUrl` and send `text`; resolves once the reply settles. */
export async function chat(page: Page, projectUrl: string, text: string, waitFor?: RegExp | string): Promise<void> {
  await page.goto(`${projectUrl}/chat`);
  await page.getByRole("button", { name: "New chat", exact: true }).first().click();
  await page.getByPlaceholder(COMPOSER).fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  if (waitFor) await expect(page.getByText(waitFor).first()).toBeVisible({ timeout: 30_000 });
}

/** The fake scanner's control files (`tests/fixtures/e2e_scan.py`). */
export function scanControl(dir = env.control) {
  return {
    delay: (sec: number | null) =>
      sec === null ? fs.rmSync(path.join(dir, "delay"), { force: true }) : fs.writeFileSync(path.join(dir, "delay"), String(sec)),
    fail: (on: boolean) =>
      on ? fs.writeFileSync(path.join(dir, "fail"), "1") : fs.rmSync(path.join(dir, "fail"), { force: true }),
  };
}

export const prodControl = () => scanControl(path.join(env.root, "prod-control"));

/**
 * A few agent calls over a local portal's HTTP MCP endpoint (`/mcp/<slug>`), exactly as
 * a coding agent makes them, so the Overview's agent activity has data. Counting happens
 * when a tool body is entered, so a tool that answers with an error still counts.
 */
export async function seedAgentCalls(request: APIRequestContext, origin: string, slug: string, rounds = 3): Promise<void> {
  const calls: [string, Record<string, unknown>][] = [
    ["whygraph_evidence_for", { path: "README.md", line_start: 1, line_end: 3 }],
    ["whygraph_area_history", { path: "README.md" }],
    ["whygraph_evidence_for", { path: "src/notes.py", line_start: 1, line_end: 5 }],
  ];
  let id = 1;
  let ok = 0;
  let last = "";
  for (let i = 0; i < rounds; i++) {
    for (const [name, args] of calls) {
      const res = await request.post(`${origin}/mcp/${slug}`, {
        headers: { Accept: "application/json, text/event-stream", "Content-Type": "application/json" },
        data: { jsonrpc: "2.0", id: id++, method: "tools/call", params: { name, arguments: args } },
      });
      if (res.status() === 200) ok++;
      else last = `${res.status()} ${(await res.text()).slice(0, 200)}`;
    }
  }
  if (!ok) throw new Error(`no MCP call to /mcp/${slug} answered 200 (${last})`);
}
