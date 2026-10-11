import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { env } from "../env";

// Helpers for the usage specs (`usage.spec.ts`, `usage-production.spec.ts`). They
// need the portals to run natively: both are pointed at the fake LLM on loopback.

/** The one model `tests/llm_fake.py` serves. */
export const STUB_MODEL = "e2e-stub-model";
/** What the fake always answers. */
export const STUB_REPLY = "The e2e stub model says hello.";

const HEADERS = { "X-WhyGraph-Client": "1" };

/** One JSON call to a portal's API, failing the test on a non-2xx answer. */
export async function call<T = unknown>(
  request: APIRequestContext,
  method: "GET" | "PUT" | "DELETE",
  url: string,
  data?: unknown,
): Promise<T> {
  const res = await request.fetch(url, { method, headers: HEADERS, data });
  expect(res.status(), `${method} ${url}: ${await res.text()}`).toBeLessThan(300);
  return (res.status() === 204 ? undefined : await res.json()) as T;
}

/**
 * Point `origin`'s organization at the fake LLM and price its model: one
 * `PUT /api/portal/defaults` with the endpoint and the key (config is saved before
 * secrets, so the key survives the endpoint change), the chat model, then a price
 * override (a custom endpoint is unpriced otherwise). Idempotent, so both colour
 * passes can run it.
 */
export async function configureStubLlm(request: APIRequestContext, origin: string): Promise<void> {
  await call(request, "PUT", `${origin}/api/portal/defaults`, {
    config: {
      llm: { openai: { base_url: env.llmUrl } },
      chat: { model: `openai/${STUB_MODEL}` },
    },
    secrets: { llm: { openai: "sk-e2e-stub" } },
  });
  await call(request, "PUT", `${origin}/api/prices`, {
    provider: "openai",
    model: STUB_MODEL,
    input_per_mtok: 5,
    output_per_mtok: 15,
  });
}

/**
 * Open a new chat (`/chat` is an empty thread), send `text` and wait for the stub's
 * reply. The session is created by the first message, and the URL moves to it.
 */
export async function chatOnce(page: Page, projectUrl: string, text: string): Promise<void> {
  await page.goto(`${projectUrl}/chat`);
  await page.getByPlaceholder("Ask about this repository…").fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByText(STUB_REPLY).first()).toBeVisible();
  await expect(page).toHaveURL(/\/chat\/\d+$/);
}

/**
 * Reload the page until `target` shows. The usage writer batches rows for up to
 * a second, so a page opened right after the call may not have them yet; `settled`
 * is what the page shows once it has loaded either way (the rows or an empty note).
 */
export async function reloadUntilVisible(page: Page, target: Locator, settled: Locator): Promise<void> {
  const deadline = Date.now() + 30_000;
  for (;;) {
    await expect(target.or(settled).first()).toBeVisible();
    if (await target.first().isVisible()) return;
    if (Date.now() > deadline) throw new Error("the usage rows never showed");
    await page.waitForTimeout(500);
    await page.reload();
  }
}
