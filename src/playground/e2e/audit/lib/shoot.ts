import fs from "node:fs";
import path from "node:path";
import type { Page, Request, Route } from "@playwright/test";
import { horizontalOverflow } from "../../lib/overflow";

// The audit's capture helpers: every `shoot()` writes four full-height PNGs
// (light / dark x desktop / phone) and one manifest line per PNG.

export const OUT = path.resolve(process.env.AUDIT_OUT ?? "e2e/.artifacts/audit");
export const SHOTS = path.join(OUT, "shots");
export const MANIFEST_LINES = path.join(OUT, "manifest.jsonl");
export const GAP_LINES = path.join(OUT, "gaps.jsonl");

export type Mode = "local" | "production";
export const WIDTHS = { desktop: { width: 1280, height: 900 }, phone: { width: 390, height: 844 } } as const;
export type Width = keyof typeof WIDTHS;
export type Theme = "light" | "dark";

/** The tallest a "full page" capture may grow (px). */
const MAX_HEIGHT = 9000;

export interface ShotMeta {
  mode: Mode;
  area: string;
  /** File-name stem of the page (kebab-case). */
  name: string;
  /** `default`, `empty`, `loading`, `error`, `running`, ... */
  state?: string;
  /** One line: what is on screen and how it was reached. */
  desc: string;
}

interface Watch {
  console: string[];
  api: string[];
  /** Answers this page is expected to get (see `expectAnswers`). */
  expected: RegExp[];
}
const watches = new WeakMap<Page, Watch>();

/**
 * Answers the audit expects, matched against `"<METHOD> <path><search> <status>"`: a
 * 401 on the import page's GitHub listing before Connect GitHub, and the expired
 * session's 401 on the project list. The deliberate error variants register theirs
 * through `expectAnswers`, so everything else >= 400 stays a flag.
 */
const EXPECTED_ANSWERS: RegExp[] = [
  /^GET \/api\/github\/installations(\/[^ ]*)? 401$/,
  /^GET \/api\/projects 401$/,
];

/** Treat answers matching `patterns` as expected on `page`; the returned function stops that. */
export function expectAnswers(page: Page, ...patterns: RegExp[]): () => void {
  watch(page);
  const w = watches.get(page)!;
  w.expected.push(...patterns);
  return () => {
    for (const p of patterns) {
      const i = w.expected.indexOf(p);
      if (i >= 0) w.expected.splice(i, 1);
    }
  };
}

/** Start collecting console errors and failed API answers of `page` (once per page). */
export function watch(page: Page): void {
  if (watches.has(page)) return;
  const w: Watch = { console: [], api: [], expected: [] };
  watches.set(page, w);
  // A shot reports only the answers of its own page: start afresh on each main-frame navigation.
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) {
      w.console.length = 0;
      w.api.length = 0;
    }
  });
  page.on("console", (m) => {
    if (m.type() === "error") w.console.push(m.text().slice(0, 300));
  });
  page.on("pageerror", (e) => w.console.push(`pageerror: ${String(e.message).slice(0, 300)}`));
  page.on("requestfailed", (r) => {
    const why = r.failure()?.errorText ?? "failed";
    if (why !== "net::ERR_ABORTED") w.console.push(`requestfailed: ${r.url().slice(0, 200)} (${why})`);
  });
  page.on("response", (r) => {
    const u = new URL(r.url());
    if (u.pathname.startsWith("/api/") && r.status() >= 400) {
      w.api.push(`${r.request().method()} ${u.pathname}${u.search} ${r.status()}`);
    }
  });
}

/** What `page` saw since the last drain, minus the expected answers (and their console echoes). */
function drain(page: Page): { console: string[]; api: string[] } {
  const w = watches.get(page) ?? { console: [], api: [], expected: [] };
  const patterns = [...EXPECTED_ANSWERS, ...w.expected];
  const api = [...new Set(w.api)];
  const kept = api.filter((a) => !patterns.some((p) => p.test(a)));
  // The browser logs "Failed to load resource ... status of N" for each failed request: drop
  // those whose status only expected answers had.
  const unexpectedStatus = new Set(kept.map((a) => a.slice(a.lastIndexOf(" ") + 1)));
  const expectedStatus = new Set(api.filter((a) => !kept.includes(a)).map((a) => a.slice(a.lastIndexOf(" ") + 1)));
  const consoleLines = [...new Set(w.console)].filter((c) => {
    const m = /Failed to load resource: the server responded with a status of (\d+)/.exec(c);
    return !(m && expectedStatus.has(m[1]) && !unexpectedStatus.has(m[1]));
  });
  w.console.length = 0;
  w.api.length = 0;
  return { console: consoleLines, api: kept };
}

const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

/** How much taller than the viewport the page's scroll containers want to be. */
async function overflowHeight(page: Page): Promise<number> {
  return page.evaluate(() => {
    let extra = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
      if (el.clientHeight < 80) continue;
      const oy = getComputedStyle(el).overflowY;
      if (oy !== "auto" && oy !== "scroll") continue;
      extra = Math.max(extra, el.scrollHeight - el.clientHeight);
    }
    return extra;
  });
}

async function settle(page: Page, ms = 350): Promise<void> {
  await page.waitForTimeout(ms);
  await page.evaluate(() => document.fonts?.ready).catch(() => undefined);
}

export interface ShootOptions {
  widths?: Width[];
  themes?: Theme[];
  /** Skip the full-height growth (e.g. a page whose content streams in). */
  viewportOnly?: boolean;
  /** Runs after each viewport / theme change, before the capture (e.g. re-open a menu). */
  before?: (page: Page, width: Width) => Promise<void>;
}

/**
 * Capture the page as it is now: desktop + phone, light + dark, each grown to its
 * content's height (the shell is `h-screen` with an inner scroller, so Playwright's
 * own `fullPage` would stop at the viewport). Ends back at desktop / light.
 */
export async function shoot(page: Page, meta: ShotMeta, opts: ShootOptions = {}): Promise<void> {
  watch(page);
  const state = meta.state ?? "default";
  const widths = opts.widths ?? ["desktop", "phone"];
  const themes = opts.themes ?? ["light", "dark"];
  const dir = path.join(SHOTS, meta.mode, slugify(meta.area));
  fs.mkdirSync(dir, { recursive: true });
  const url = page.url();
  const seen = drain(page);
  for (const width of widths) {
    const base = WIDTHS[width];
    await page.setViewportSize(base);
    for (const theme of themes) {
      await page.emulateMedia({ colorScheme: theme });
      await settle(page);
      if (opts.before) await opts.before(page, width);
      let height: number = base.height;
      if (!opts.viewportOnly) {
        const extra = await overflowHeight(page).catch(() => 0);
        if (extra > 4) {
          height = Math.min(MAX_HEIGHT, base.height + extra);
          await page.setViewportSize({ width: base.width, height });
          await settle(page, 250);
          const more = await overflowHeight(page).catch(() => 0);
          if (more > 4 && height < MAX_HEIGHT) {
            height = Math.min(MAX_HEIGHT, height + more);
            await page.setViewportSize({ width: base.width, height });
            await settle(page, 200);
          }
        }
      }
      const xs = width === "phone" ? await horizontalOverflow(page).catch(() => []) : [];
      const file = `${slugify(meta.name)}__${slugify(state)}__${theme}__${width}.png`;
      const out = path.join(dir, file);
      await page.screenshot({ path: out, fullPage: true, animations: "disabled", caret: "hide" });
      if (height !== base.height) await page.setViewportSize(base);
      const after = drain(page);
      const entry = {
        file: path.relative(OUT, out),
        mode: meta.mode,
        area: meta.area,
        page: meta.name,
        url,
        route: safePath(url),
        state,
        theme,
        width,
        viewport: `${base.width}x${base.height}`,
        captured_height: height,
        description: meta.desc,
        ...(seen.console.length + after.console.length ? { console_errors: [...seen.console, ...after.console] } : {}),
        ...(seen.api.length + after.api.length ? { api_errors: [...seen.api, ...after.api] } : {}),
        ...(xs.length ? { overflow_x: xs } : {}),
      };
      seen.console = [];
      seen.api = [];
      fs.appendFileSync(MANIFEST_LINES, JSON.stringify(entry) + "\n");
    }
  }
  await page.setViewportSize(WIDTHS.desktop);
  await page.emulateMedia({ colorScheme: "light" });
}

function safePath(url: string): string {
  try {
    const u = new URL(url);
    return `${u.host}${u.pathname}${u.search}`;
  } catch {
    return url;
  }
}

/** Record something the audit could not capture, and why. */
export function gap(mode: Mode, area: string, name: string, state: string, reason: string): void {
  fs.mkdirSync(OUT, { recursive: true });
  fs.appendFileSync(GAP_LINES, JSON.stringify({ mode, area, page: name, state, reason }) + "\n");
}

/**
 * Run `step`, and record a gap instead of failing the whole audit when it throws:
 * one broken screen must not cost every screen after it.
 */
export async function attempt(
  page: Page,
  where: { mode: Mode; area: string; name: string; state?: string },
  step: () => Promise<void>,
): Promise<boolean> {
  try {
    await step();
    return true;
  } catch (e) {
    const msg = String((e as Error).message ?? e).split("\n")[0].slice(0, 300);
    gap(where.mode, where.area, where.name, where.state ?? "default", `step failed: ${msg}`);
    // Keep evidence of the failure next to the shots.
    const dir = path.join(OUT, "failures");
    fs.mkdirSync(dir, { recursive: true });
    await page
      .screenshot({ path: path.join(dir, `${where.mode}-${slugify(where.area)}-${slugify(where.name)}-${slugify(where.state ?? "default")}.png`) })
      .catch(() => undefined);
    await page.setViewportSize(WIDTHS.desktop).catch(() => undefined);
    await page.emulateMedia({ colorScheme: "light" }).catch(() => undefined);
    return false;
  }
}

// ---- request interception: loading and error variants ----------------------

const isApi = (req: Request) => new URL(req.url()).pathname.startsWith("/api/");

/** The requests a page needs to render its shell at all; never held or failed. */
export function keepShell(extra: RegExp[] = []) {
  const keep = [/^\/api\/portal\/state$/, /^\/api\/auth\//, /^\/api\/account$/, ...extra];
  return (req: Request) => {
    if (!isApi(req) || req.method() !== "GET") return false;
    const p = new URL(req.url()).pathname;
    return !keep.some((k) => k.test(p));
  };
}

/**
 * Hold every request `match` accepts until the returned `release()` is called,
 * so a page can be captured in its loading state.
 */
export async function holdRequests(page: Page, match: (req: Request) => boolean): Promise<() => Promise<void>> {
  let open!: () => void;
  const gate = new Promise<void>((r) => (open = r));
  const handler = async (route: Route) => {
    if (!match(route.request())) return route.fallback();
    await gate;
    await route.continue().catch(() => undefined);
  };
  await page.route("**/api/**", handler);
  return async () => {
    open();
    await page.unroute("**/api/**", handler);
  };
}

/**
 * Answer every API request (including the shell's own) with a 500, for the root error
 * boundary; registers the answers as expected. Returns the undo function.
 */
export async function failAllRequests500(page: Page): Promise<() => Promise<void>> {
  const stop = expectAnswers(page, /^[A-Z]+ \S+ 500$/);
  const handler = (route: Route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "Internal Server Error" }) });
  await page.route("**/api/**", handler);
  return async () => {
    stop();
    await page.unroute("**/api/**", handler);
  };
}

/** Answer every request `match` accepts with `status` and a portal-shaped error body. */
export async function failRequests(
  page: Page,
  match: (req: Request) => boolean,
  status = 500,
  body: Record<string, unknown> = { detail: "Internal Server Error" },
): Promise<() => Promise<void>> {
  const handler = async (route: Route) => {
    if (!match(route.request())) return route.fallback();
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  };
  await page.route("**/api/**", handler);
  return async () => {
    await page.unroute("**/api/**", handler);
  };
}

export interface VariantOptions {
  /** Requests to hold / fail; default: every API GET but the shell's. */
  match?: (req: Request) => boolean;
  /** Which variants; default loading + error (500). */
  states?: ("loading" | "error" | "forbidden")[];
  /** Wait this long for the error state to render (react-query retries once). */
  errorWaitMs?: number;
}

/** Capture `url` while its data is held (loading) and when it fails (500 / coded 403). */
export async function variants(page: Page, url: string, meta: Omit<ShotMeta, "state" | "desc"> & { what: string }, opts: VariantOptions = {}): Promise<void> {
  const match = opts.match ?? keepShell();
  for (const state of opts.states ?? ["loading", "error"]) {
    await attempt(page, { ...meta, state }, async () => {
      if (state === "loading") {
        const release = await holdRequests(page, match);
        try {
          await page.goto(url, { waitUntil: "domcontentloaded" });
          await page.waitForTimeout(1200);
          await shoot(page, { ...meta, state, desc: `${meta.what}, with its API requests held open (route intercept) to show the loading state` }, { viewportOnly: true });
        } finally {
          await release();
        }
      } else {
        const body =
          state === "error"
            ? { detail: "Internal Server Error" }
            : { code: "forbidden", detail: "You do not have permission to do that." };
        const restore = await failRequests(page, match, state === "error" ? 500 : 403, body);
        const stopExpecting = expectAnswers(page, state === "error" ? /^GET \S+ 500$/ : /^GET \S+ 403$/);
        try {
          await page.goto(url, { waitUntil: "domcontentloaded" });
          await page.waitForTimeout(opts.errorWaitMs ?? 3500);
          await shoot(page, {
            ...meta,
            state,
            desc:
              state === "error"
                ? `${meta.what}, with its API GETs answered 500 {"detail":"Internal Server Error"} (route intercept)`
                : `${meta.what}, with its API GETs answered 403 {"code":"forbidden"} (route intercept)`,
          });
        } finally {
          stopExpecting();
          await restore();
        }
      }
    });
  }
}
