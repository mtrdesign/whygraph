// WhyGraph UI capture: screenshots, ARIA snapshots and console / network
// signals of a local app, before and after a change.
//
//   node capture.mjs --tag <before|after> --out <dir> [--reset] <route>...
//   node capture.mjs --probe
//
// Exit codes: 0 done, 2 a gate fired (one stderr line per new signal),
// 3 capture unavailable (one stderr line with the reason), 4 usage error.
//
// Every setting that widens what the script does (base URL, start command,
// allowed hosts, masks) comes only from the untracked profile
// `.whygraph/profiles/ui.md` in the working directory. The script uses the
// project's own Playwright (1.49 or newer) and never installs anything.

import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  closeSync,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const PROFILE_PATH = '.whygraph/profiles/ui.md';
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);
const ROUTE_PATTERN = /^\/[A-Za-z0-9._~/%-]*$/;
const LIST_KEYS = new Set([
  'derived_from',
  'frontend_paths',
  'allow_hosts',
  'mask',
  'ignore_console',
  'ignore_requests',
]);
const UNNAMED_LINE =
  /^\s*- (button|link|textbox|checkbox|radio|combobox|switch|tab|menuitem|slider|searchbox|spinbutton)(?: \[[^\]]*\])?:?$/;
const WIDTHS = [
  [1280, 900],
  [390, 844],
];
const MIN_PLAYWRIGHT = [1, 49];
const NO_MOTION =
  '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
const CSP =
  "default-src 'none'; img-src 'self' file: data:; style-src 'unsafe-inline'";
const INDEX_MARK = '<!-- whygraph-capture-index -->';

// ---------------------------------------------------------------- pure parts

/** True when `url` is http(s), has no userinfo and names a loopback or allowed host. */
export function isAllowedUrl(url, allowHosts = []) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
  if (parsed.username || parsed.password) return false;
  const host = parsed.hostname.toLowerCase();
  if (LOOPBACK.has(host)) return true;
  return (allowHosts || []).some((h) => String(h).trim().toLowerCase() === host);
}

/** True when `route` matches plan 44 D21's pattern and is not protocol-relative. */
export function validRoute(route) {
  return typeof route === 'string' && ROUTE_PATTERN.test(route) && !route.startsWith('//');
}

/** Resolve `route` against `base`; throws when the result leaves the base origin. */
export function resolveRoute(route, base) {
  const origin = new URL(base).origin;
  const url = new URL(route, base);
  if (url.origin !== origin) throw new Error(`route leaves the base origin: ${route}`);
  return url;
}

/** A file-name-safe, collision-free name for a route: `<sanitised>-<8 hex of SHA-1>`. */
export function sanitizeRoute(route) {
  const stem = String(route).replace(/^\//, '').replace(/[^A-Za-z0-9._-]/g, '-').slice(0, 100) || 'root';
  const hash = createHash('sha1').update(String(route)).digest('hex').slice(0, 8);
  return `${stem}-${hash}`;
}

/** Escape text for HTML element content and attribute values. */
export function escapeHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Replace every match of every pattern (regular-expression strings) with `***`. */
export function maskAria(text, patterns = []) {
  let out = String(text);
  for (const pattern of patterns || []) {
    if (pattern) out = out.replace(new RegExp(pattern, 'g'), '***');
  }
  return out;
}

/** Count unnamed interactive elements per role in an ARIA snapshot. */
export function unnamedCounts(snapshot) {
  const counts = {};
  for (const line of String(snapshot).split('\n')) {
    const m = UNNAMED_LINE.exec(line);
    if (m) counts[m[1]] = (counts[m[1]] || 0) + 1;
  }
  return counts;
}

/** Normalise a console message: drop line / column, URL queries and fragments, file hashes. */
export function normaliseConsole(text) {
  return String(text)
    .replace(/\b([a-z][a-z0-9+.-]*:\/\/[^\s?#'"()<>]*)[?#][^\s'"()<>]*/gi, '$1')
    .replace(/:\d+:\d+(?=[)\s,]|$)/g, '')
    .replace(/:\d+(?=\))/g, '')
    .replace(/[-_.]?\b[0-9a-f]{8,}(?=\.[A-Za-z0-9]{1,8}\b)/gi, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 500);
}

function requestKey(r) {
  return `${r.method} ${r.path} ${r.status}`;
}

/** Signals in `after` that `before` (same route, theme and width) did not have. */
export function newSignals(before, after) {
  if (!before || !after) return [];
  const out = [];
  const seenConsole = new Set((before.console_errors || []).map(normaliseConsole));
  for (const msg of after.console_errors || []) {
    const norm = normaliseConsole(msg);
    if (!seenConsole.has(norm)) {
      seenConsole.add(norm);
      out.push(`console error: ${norm}`);
    }
  }
  const seenRequests = new Set((before.failed_requests || []).map(requestKey));
  for (const req of after.failed_requests || []) {
    const key = requestKey(req);
    if (!seenRequests.has(key)) {
      seenRequests.add(key);
      out.push(`failed request: ${key}`);
    }
  }
  if (after.overflow && !before.overflow) out.push('horizontal overflow');
  const was = before.unnamed || {};
  for (const [role, count] of Object.entries(after.unnamed || {})) {
    if (count > (was[role] || 0)) out.push(`unnamed ${role}: ${was[role] || 0} -> ${count}`);
  }
  return out;
}

/** Parse the profile: frontmatter and `key: value` lines; list values split on `; `. */
export function parseProfile(text) {
  const profile = {};
  for (const raw of String(text).split('\n')) {
    const line = raw.replace(/\r$/, '');
    const m = /^([a-z_]+):(?:\s(.*))?$/.exec(line);
    if (!m) continue;
    const value = (m[2] || '').trim();
    profile[m[1]] = LIST_KEYS.has(m[1])
      ? value
          .split('; ')
          .map((v) => v.trim())
          .filter(Boolean)
      : value;
  }
  for (const key of LIST_KEYS) if (!(key in profile)) profile[key] = [];
  return profile;
}

// ----------------------------------------------------------------- CLI parts

class Unavailable extends Error {}
class UsageError extends Error {}

const state = { server: null, pidFile: null };

function say(line) {
  process.stdout.write(`${line}\n`);
}

function firstLine(err) {
  return String((err && err.message) || err).split('\n')[0];
}

function isMain() {
  try {
    return Boolean(
      process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url)),
    );
  } catch {
    return false;
  }
}

function parseArgs(argv) {
  const args = { probe: false, tag: null, out: null, reset: false, routes: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--probe') args.probe = true;
    else if (a === '--reset') args.reset = true;
    else if (a === '--tag' || a === '--out') {
      if (i + 1 >= argv.length) throw new UsageError(`${a} needs a value`);
      args[a.slice(2)] = argv[(i += 1)];
    } else if (a.startsWith('--')) throw new UsageError(`unknown option: ${a}`);
    else args.routes.push(a);
  }
  if (args.probe) {
    if (args.tag || args.out || args.reset || args.routes.length) {
      throw new UsageError('--probe takes no other argument');
    }
    return args;
  }
  if (args.tag !== 'before' && args.tag !== 'after') throw new UsageError('--tag must be before or after');
  if (!args.out) throw new UsageError('--out <dir> is required');
  if (!args.routes.length) throw new UsageError('at least one route is required');
  for (const r of args.routes) if (!validRoute(r)) throw new UsageError(`invalid route: ${r}`);
  return args;
}

function insideRoot(root, rel, what) {
  const abs = resolve(root, rel || '.');
  const r = relative(root, abs);
  if (r.startsWith('..') || isAbsolute(r)) throw new Unavailable(`${what} leaves the repo: ${rel}`);
  return abs;
}

function compilePatterns(list, key) {
  return list.map((p) => {
    try {
      return new RegExp(p);
    } catch {
      throw new Unavailable(`invalid ${key} pattern in the profile: ${p}`);
    }
  });
}

function loadProfile(root) {
  let path = root;
  for (const part of PROFILE_PATH.split('/')) {
    path = join(path, part);
    let st;
    try {
      st = lstatSync(path);
    } catch {
      throw new Unavailable(`no UI profile at ${PROFILE_PATH}`);
    }
    if (st.isSymbolicLink()) throw new Unavailable(`the profile path goes through a symlink: ${PROFILE_PATH}`);
  }
  const git = spawnSync('git', ['ls-files', '--error-unmatch', PROFILE_PATH], { cwd: root, stdio: 'ignore' });
  if (git.status === 0) throw new Unavailable('profile is tracked by git');
  const profile = parseProfile(readFileSync(path, 'utf8'));
  if (profile.auth === 'required') throw new Unavailable('the app requires a login (auth: required)');
  if (!profile.base_url) throw new Unavailable('the profile has no base_url');
  if (!isAllowedUrl(profile.base_url, profile.allow_hosts)) {
    throw new Unavailable(`base_url is not on an allowed host: ${profile.base_url}`);
  }
  compilePatterns(profile.mask, 'mask');
  profile.consoleIgnore = compilePatterns(profile.ignore_console, 'ignore_console');
  profile.requestIgnore = compilePatterns(profile.ignore_requests, 'ignore_requests');
  profile.dirs = [...new Set([root, ...profile.frontend_paths.map((p) => insideRoot(root, p, 'frontend_paths'))])];
  return profile;
}

function versionAtLeast(version, [major, minor]) {
  const [a, b] = String(version).split('.').map((n) => parseInt(n, 10));
  return a > major || (a === major && b >= minor);
}

function loadPlaywright(root, profile) {
  const target = profile.frontend_paths[0] || '.';
  for (const dir of profile.dirs) {
    const req = createRequire(join(dir, 'package.json'));
    for (const name of ['playwright', '@playwright/test', 'playwright-core']) {
      let version;
      try {
        version = req(`${name}/package.json`).version;
      } catch {
        continue;
      }
      const shown = relative(root, dir) || '.';
      if (!versionAtLeast(version, MIN_PLAYWRIGHT)) {
        throw new Unavailable(
          `${name} ${version} in ${shown} is older than 1.49: run "npm i -D playwright@latest && npx playwright@latest install chromium" in ${shown}`,
        );
      }
      const lib = req(name);
      const chromium = lib.chromium || (lib.default && lib.default.chromium);
      if (!chromium) throw new Unavailable(`${name} ${version} in ${shown} exposes no chromium`);
      let exe = '';
      try {
        exe = chromium.executablePath();
      } catch {
        exe = '';
      }
      if (!exe || !existsSync(exe)) {
        throw new Unavailable(
          `chromium for playwright ${version} is not installed: run "npx playwright@${version} install chromium" in ${shown}`,
        );
      }
      return { chromium, version };
    }
  }
  const dirs = profile.dirs.map((d) => relative(root, d) || '.').join(', ');
  throw new Unavailable(
    `playwright not found in ${dirs}: run "npm i -D playwright@latest && npx playwright@latest install chromium" in ${target}`,
  );
}

async function answers(base, ms) {
  try {
    await fetch(base, { redirect: 'manual', signal: AbortSignal.timeout(ms) });
    return true;
  } catch {
    return false;
  }
}

function groupAlive(pid) {
  try {
    process.kill(process.platform === 'win32' ? pid : -pid, 0);
    return true;
  } catch {
    return false;
  }
}

function killGroup(pid, signal) {
  try {
    if (process.platform === 'win32') spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
    else process.kill(-pid, signal);
  } catch {
    // already gone
  }
}

async function stopServer() {
  const child = state.server;
  if (!child) return;
  state.server = null;
  killGroup(child.pid, 'SIGTERM');
  for (let i = 0; i < 50 && groupAlive(child.pid); i += 1) await new Promise((r) => setTimeout(r, 100));
  if (groupAlive(child.pid)) killGroup(child.pid, 'SIGKILL');
  if (state.pidFile) rmSync(state.pidFile, { force: true });
}

async function ensureServer(root, profile, out) {
  const base = profile.base_url;
  const pidFile = join(dirname(resolve(out)), '.server.pid');
  if (existsSync(pidFile)) {
    const old = parseInt(readFileSync(pidFile, 'utf8'), 10);
    if (old && !groupAlive(old)) {
      say(`note: the dev server this script started earlier (pid ${old}) is gone; removing its pid file`);
    } else if (old) {
      say(`note: a dev server this script started earlier may still run (pid ${old}); stop it by hand if it is stale`);
    }
    rmSync(pidFile, { force: true });
  }
  if (await answers(base, 2000)) {
    say(`server: reusing ${base}`);
    return;
  }
  const command = profile.fixture && profile.fixture !== 'none' ? profile.fixture : profile.start;
  if (!command || command === 'none') {
    throw new Unavailable(`${base} does not answer and the profile has no start command`);
  }
  const cwd = insideRoot(root, profile.start_dir, 'start_dir');
  const logPath = join(dirname(pidFile), '.server.log');
  mkdirSync(dirname(pidFile), { recursive: true });
  const log = openSync(logPath, 'w');
  const child = spawn(command, { shell: true, cwd, detached: true, stdio: ['ignore', log, log] });
  closeSync(log);
  let exited = null;
  child.on('exit', (code, signal) => {
    exited = signal || code;
  });
  child.on('error', (err) => {
    exited = firstLine(err);
  });
  state.server = child;
  state.pidFile = pidFile;
  writeFileSync(pidFile, `${child.pid}\n`);
  say(`server: started "${command}" (pid ${child.pid})`);
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    if (exited !== null) {
      throw new Unavailable(`the dev server exited (${exited}) before ${base} answered; log: ${relative(root, logPath)}`);
    }
    if (await answers(base, 2000)) return;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Unavailable(`the dev server did not answer at ${base} within 60 s; log: ${relative(root, logPath)}`);
}

function parseTheme(value) {
  const v = (value || 'prefers-color-scheme').trim();
  if (v === 'none') return { mode: 'none', themes: ['light'] };
  let m = /^attribute ([A-Za-z_:][-A-Za-z0-9_:.]*)(?:=(.*))?$/.exec(v);
  if (m) return { mode: 'attribute', name: m[1], value: m[2] || '', themes: ['light', 'dark'] };
  m = /^class ([-A-Za-z0-9_]+)$/.exec(v);
  if (m) return { mode: 'class', name: m[1], value: '', themes: ['light', 'dark'] };
  return { mode: 'prefers-color-scheme', themes: ['light', 'dark'] };
}

// Top-level navigation must pass isAllowedUrl and stay on the base origin or an
// allow_hosts host: another loopback port is another app, not the one under test.
function mainFrameAllowed(url, profile) {
  if (!isAllowedUrl(url, profile.allow_hosts)) return false;
  const parsed = new URL(url);
  if (parsed.origin === new URL(profile.base_url).origin) return true;
  return profile.allow_hosts.some((h) => String(h).trim().toLowerCase() === parsed.hostname.toLowerCase());
}

function navigationAllowed(request, profile) {
  let top = false;
  try {
    top = request.frame() === request.frame().page().mainFrame();
  } catch {
    top = false;
  }
  return top ? mainFrameAllowed(request.url(), profile) : isAllowedUrl(request.url(), profile.allow_hosts);
}

function hostOf(url) {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

async function captureVariant(browser, ctx, v) {
  const { profile, theme, out, tag } = ctx;
  const [width, height] = v.size;
  const dark = v.theme === 'dark';
  const context = await browser.newContext({
    viewport: { width, height },
    colorScheme: dark ? 'dark' : 'light',
    reducedMotion: 'reduce',
    serviceWorkers: 'block',
    acceptDownloads: false,
  });
  const files = {
    png: join(out, tag, `${v.name}__${v.theme}__${width}.png`),
    aria: join(out, tag, `${v.name}__${v.theme}__${width}.aria.txt`),
  };
  const removeFiles = () => {
    rmSync(files.png, { force: true });
    rmSync(files.aria, { force: true });
  };
  let left = null;
  const check = (url) => {
    if (!left && !mainFrameAllowed(url, profile)) left = hostOf(url);
    if (left) {
      removeFiles();
      throw new Unavailable(`navigation left the allowed hosts: ${left}`);
    }
  };
  try {
    await context.route('**/*', (route) => {
      const request = route.request();
      if (request.isNavigationRequest() && !navigationAllowed(request, profile)) {
        if (!left) left = hostOf(request.url());
        return route.abort('blockedbyclient');
      }
      return route.continue();
    });
    await context.addInitScript(
      ({ css, mode, name, value, isDark }) => {
        document.addEventListener(
          'DOMContentLoaded',
          () => {
            const style = document.createElement('style');
            style.textContent = css;
            (document.head || document.documentElement).appendChild(style);
            const root = document.documentElement;
            if (mode === 'attribute') {
              if (isDark) root.setAttribute(name, value);
              else if (root.getAttribute(name) === value) root.removeAttribute(name);
            } else if (mode === 'class') root.classList.toggle(name, isDark);
          },
          { once: true },
        );
      },
      { css: NO_MOTION, mode: theme.mode, name: theme.name || '', value: theme.value || '', isDark: dark },
    );
    const page = await context.newPage();
    page.setDefaultTimeout(60000);
    const consoleErrors = new Set();
    const failed = new Map();
    const ignoredConsole = (msg) => profile.consoleIgnore.some((re) => re.test(msg));
    const ignoredRequest = (url) => {
      let path = url;
      let bare = url;
      try {
        const u = new URL(url);
        path = u.pathname;
        bare = `${u.origin}${u.pathname}`;
      } catch {
        // keep the raw text
      }
      return profile.requestIgnore.some((re) => re.test(path) || re.test(bare));
    };
    const addConsole = (msg) => {
      if (!ignoredConsole(msg)) consoleErrors.add(normaliseConsole(msg));
    };
    const addRequest = (request, status) => {
      const url = request.url();
      if (ignoredRequest(url)) return;
      let path = url;
      try {
        path = new URL(url).pathname;
      } catch {
        // keep the raw text
      }
      const entry = { method: request.method(), path, status };
      failed.set(requestKey(entry), entry);
    };
    page.on('console', (msg) => {
      // "Failed to load resource" carries no URL; the response listener records the request.
      if (msg.type() === 'error' && !/^Failed to load resource\b/.test(msg.text())) addConsole(msg.text());
    });
    page.on('pageerror', (err) => addConsole(String((err && err.message) || err)));
    page.on('response', (res) => {
      if (res.status() >= 400) addRequest(res.request(), res.status());
    });
    page.on('requestfailed', (request) => {
      const reason = (request.failure() && request.failure().errorText) || '';
      if (!/ERR_ABORTED/.test(reason)) addRequest(request, 'failed');
    });
    page.on('framenavigated', (frame) => {
      if (frame === page.mainFrame() && !left && !mainFrameAllowed(frame.url(), profile)) {
        left = hostOf(frame.url());
      }
    });

    try {
      await page.goto(v.url, { waitUntil: 'load', timeout: 60000 });
    } catch (err) {
      check(page.url());
      throw new Unavailable(`could not load ${v.route}: ${firstLine(err)}`);
    }
    check(page.url());
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
    if (profile.wait_for && profile.wait_for !== 'none') {
      await page.waitForSelector(profile.wait_for, { timeout: 15000 }).catch(() => {
        say(`note: wait_for "${profile.wait_for}" did not appear on ${v.route} (${v.theme}, ${width})`);
      });
    }
    check(page.url());
    await page.screenshot({ path: files.png, fullPage: true, animations: 'disabled' });
    const raw = await page.locator('body').ariaSnapshot({ timeout: 30000 });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    check(page.url());
    writeFileSync(files.aria, `${maskAria(raw, profile.mask)}\n`);
    return {
      tag,
      route: v.route,
      theme: v.theme,
      width,
      console_errors: [...consoleErrors],
      failed_requests: [...failed.values()],
      overflow,
      unnamed: unnamedCounts(raw),
      png: relative(out, files.png).split('\\').join('/'),
      aria: relative(out, files.aria).split('\\').join('/'),
    };
  } finally {
    await context.close().catch(() => {});
  }
}

function sameVariant(a, b) {
  return a.route === b.route && a.theme === b.theme && a.width === b.width;
}

function readSignals(out) {
  try {
    const data = JSON.parse(readFileSync(join(out, 'signals.json'), 'utf8'));
    return Array.isArray(data) ? data : [];
  } catch {
    return [];
  }
}

function writeSignals(out, entries, tag) {
  const kept = readSignals(out).filter((e) => !entries.some((n) => n.tag === e.tag && sameVariant(n, e)));
  writeFileSync(join(out, 'signals.json'), `${JSON.stringify([...kept, ...entries], null, 2)}\n`);
  return [...kept, ...entries].filter((e) => e.tag === tag);
}

function readText(path) {
  try {
    return readFileSync(path, 'utf8');
  } catch {
    return null;
  }
}

function page(title, body) {
  return [
    '<!doctype html>',
    '<html lang="en"><head><meta charset="utf-8">',
    `<meta http-equiv="Content-Security-Policy" content="${CSP}">`,
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    `<title>${escapeHtml(title)}</title>`,
    '<style>body{font:14px/1.4 system-ui,sans-serif;margin:16px;color:#222;background:#fff}',
    'table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;vertical-align:top;text-align:left}',
    'img{max-width:100%;border:1px solid #ddd}.bad{color:#b00020}.flaky{color:#8a6d00}code{font-size:12px}',
    '@media (prefers-color-scheme: dark){body{color:#ddd;background:#111}td,th{border-color:#444}.bad{color:#ff6b81}}</style>',
    '</head><body>',
    body,
    '</body></html>',
    '',
  ].join('\n');
}

function writeSheet(out, results, hasBefore, diff) {
  const step = escapeHtml(relative(dirname(resolve(out)), resolve(out)) || out);
  const rows = results.map(({ entry, before, gated, flaky }) => {
    const label = `<code>${escapeHtml(entry.route)}</code> - ${escapeHtml(entry.theme)} - ${entry.width}px`;
    const status = diff.changed.some((d) => sameVariant(d, entry))
      ? 'ARIA changed'
      : diff.unchanged.some((d) => sameVariant(d, entry))
        ? 'ARIA unchanged'
        : 'no before capture';
    const img = (e) => (e ? `<a href="${escapeHtml(e.png)}"><img src="${escapeHtml(e.png)}" alt=""></a>` : 'no before capture');
    const list = (items, cls) =>
      items.length ? `<ul class="${cls}">${items.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ul>` : '';
    const all = [
      ...entry.console_errors.map((m) => `console error: ${m}`),
      ...entry.failed_requests.map((r) => `failed request: ${requestKey(r)}`),
      ...(entry.overflow ? ['horizontal overflow'] : []),
      ...Object.entries(entry.unnamed).map(([role, n]) => `unnamed ${role}: ${n}`),
    ];
    return [
      `<tr><th colspan="2">${label} - ${status}</th></tr>`,
      `<tr><td>${img(before)}</td><td>${img(entry)}</td></tr>`,
      '<tr><td colspan="2">',
      gated.length ? `<strong class="bad">New signals</strong>${list(gated, 'bad')}` : '',
      flaky.length ? `<strong class="flaky">Flaky (did not repeat)</strong>${list(flaky, 'flaky')}` : '',
      all.length ? `All signals after${list(all, '')}` : 'No signals after.',
      '</td></tr>',
    ].join('\n');
  });
  const body = [
    `<h1>UI capture - step ${step}</h1>`,
    hasBefore ? '' : '<p class="bad">no before capture: nothing was gated.</p>',
    '<table><tr><th>Before</th><th>After</th></tr>',
    ...rows,
    '</table>',
    '<h2>Limitations</h2>',
    '<p>Full-page screenshots miss content inside inner scroll containers; the ARIA snapshots (<code>*.aria.txt</code>) still cover it. Changed routes come from the masked ARIA snapshot, not from pixels.</p>',
  ].join('\n');
  writeFileSync(join(out, 'sheet.html'), page(`UI capture - step ${relative(dirname(resolve(out)), resolve(out))}`, body));
}

function writeIndex(out) {
  const parent = dirname(resolve(out));
  const path = join(parent, 'index.html');
  const existing = readText(path);
  if (existing !== null && !existing.includes(INDEX_MARK)) {
    say(`note: ${path} was not written by this script; index not regenerated`);
    return null;
  }
  const steps = readdirSync(parent, { withFileTypes: true })
    .filter((d) => d.isDirectory() && existsSync(join(parent, d.name, 'sheet.html')))
    .map((d) => d.name)
    .sort((a, b) => a.localeCompare(b, 'en', { numeric: true }));
  const body = [
    INDEX_MARK,
    '<h1>UI captures</h1>',
    '<ul>',
    ...steps.map((s) => `<li><a href="${escapeHtml(`${s}/sheet.html`)}">Step ${escapeHtml(s)}</a></li>`),
    '</ul>',
  ].join('\n');
  writeFileSync(path, page('UI captures', body));
  return path;
}

async function capture(args) {
  const root = process.cwd();
  const out = resolve(root, args.out);
  const marker = join(out, 'before', '.complete');
  if (args.tag === 'before' && !args.reset && existsSync(marker)) {
    say(`before: kept (captured ${readFileSync(marker, 'utf8').trim()})`);
    return 0;
  }
  const profile = loadProfile(root);
  const { chromium } = loadPlaywright(root, profile);
  const theme = parseTheme(profile.theme);
  const variants = [];
  for (const route of args.routes) {
    let url;
    try {
      url = resolveRoute(route, profile.base_url).href;
    } catch (err) {
      throw new UsageError(firstLine(err));
    }
    for (const t of theme.themes) {
      for (const size of WIDTHS) variants.push({ route, url, theme: t, size, name: sanitizeRoute(route) });
    }
  }
  if (args.tag === 'before' && args.reset) {
    rmSync(join(out, 'before'), { recursive: true, force: true });
    const kept = readSignals(out).filter((e) => e.tag !== 'before');
    mkdirSync(out, { recursive: true });
    writeFileSync(join(out, 'signals.json'), `${JSON.stringify(kept, null, 2)}\n`);
  }
  mkdirSync(join(out, args.tag), { recursive: true });
  await ensureServer(root, profile, args.out);

  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (err) {
    throw new Unavailable(`chromium did not start: ${firstLine(err)}`);
  }
  const ctx = { profile, theme, out, tag: args.tag };
  const hasBefore = existsSync(marker);
  const beforeEntries = hasBefore ? readSignals(out).filter((e) => e.tag === 'before') : [];
  const results = [];
  try {
    for (const v of variants) {
      let entry;
      try {
        entry = await captureVariant(browser, ctx, v);
      } catch (err) {
        if (err instanceof Unavailable && /^navigation left/.test(err.message)) {
          const key = { route: v.route, theme: v.theme, width: v.size[0] };
          const kept = readSignals(out).filter((e) => !(e.tag === args.tag && sameVariant(e, key)));
          writeFileSync(join(out, 'signals.json'), `${JSON.stringify(kept, null, 2)}\n`);
        }
        throw err;
      }
      const before = beforeEntries.find((e) => sameVariant(e, entry)) || null;
      let gated = [];
      let flaky = [];
      if (args.tag === 'after' && before) {
        const first = newSignals(before, entry);
        if (first.length) {
          entry = await captureVariant(browser, ctx, v);
          const again = new Set(newSignals(before, entry));
          gated = first.filter((s) => again.has(s));
          flaky = first.filter((s) => !again.has(s));
        }
      }
      results.push({ entry, before, gated, flaky });
    }
  } finally {
    await browser.close().catch(() => {});
  }
  writeSignals(
    out,
    results.map((r) => r.entry),
    args.tag,
  );

  if (args.tag === 'before') {
    writeFileSync(marker, `${new Date().toISOString()}\n`);
    say(`before: captured ${results.length} variants in ${relative(root, join(out, 'before')) || '.'}`);
    return 0;
  }

  const diff = { changed: [], unchanged: [], no_before: [] };
  for (const { entry, before } of results) {
    const key = { route: entry.route, theme: entry.theme, width: entry.width };
    const was = before ? readText(join(out, before.aria)) : null;
    if (was === null) diff.no_before.push(key);
    else if (was === readText(join(out, entry.aria))) diff.unchanged.push(key);
    else diff.changed.push(key);
  }
  writeFileSync(join(out, 'diff.json'), `${JSON.stringify(diff, null, 2)}\n`);
  writeSheet(out, results, hasBefore, diff);
  const index = writeIndex(args.out);
  say(`after: captured ${results.length} variants in ${relative(root, join(out, 'after')) || '.'}`);
  say(`changed: ${diff.changed.length}, unchanged: ${diff.unchanged.length}, no before: ${diff.no_before.length}`);
  say(`sheet: ${relative(root, join(out, 'sheet.html'))}`);
  if (index) say(`index: ${relative(root, index)}`);
  for (const { entry, flaky } of results) {
    for (const s of flaky) say(`flaky: ${entry.route} ${entry.theme} ${entry.width}: ${s}`);
  }
  const gates = results.flatMap(({ entry, gated }) => gated.map((s) => `${entry.route} ${entry.theme} ${entry.width}: ${s}`));
  for (const line of gates) process.stderr.write(`${line}\n`);
  return gates.length ? 2 : 0;
}

function probe() {
  const root = process.cwd();
  try {
    const profile = loadProfile(root);
    loadPlaywright(root, profile);
    say('capture: available');
    return 0;
  } catch (err) {
    if (err instanceof Unavailable) {
      say(`capture: unavailable: ${err.message}`);
      return 3;
    }
    throw err;
  }
}

let finishing = null;

async function finish(code, message) {
  if (finishing) return finishing;
  finishing = new Promise(() => {});
  if (message) process.stderr.write(`${message}\n`);
  await stopServer().catch(() => {});
  process.exit(code);
}

async function main() {
  process.on('SIGINT', () => finish(130, 'capture interrupted (SIGINT)'));
  process.on('SIGTERM', () => finish(143, 'capture interrupted (SIGTERM)'));
  process.on('uncaughtException', (err) => finish(1, `capture error: ${firstLine(err)}`));
  process.on('unhandledRejection', (err) => finish(1, `capture error: ${firstLine(err)}`));
  process.on('exit', () => {
    if (state.server) killGroup(state.server.pid, 'SIGKILL');
  });
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (err) {
    return finish(4, `usage: ${err.message}`);
  }
  try {
    const code = args.probe ? probe() : await capture(args);
    return finish(code);
  } catch (err) {
    if (err instanceof UsageError) return finish(4, `usage: ${err.message}`);
    if (err instanceof Unavailable) return finish(3, err.message);
    return finish(1, `capture error: ${firstLine(err)}`);
  }
}

if (isMain()) main();
