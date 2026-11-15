// Tests for the UI capture script's pure exports (plan 45, section 9 case 6).
// Run through Node's built-in runner: `node --test tests/js/capture.test.mjs`.
// Nothing here needs Playwright or a browser: the script only loads Playwright
// inside its CLI path, behind a main guard.

import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from 'node:fs';
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  childEnv,
  copyShots,
  e2eDeadline,
  escapeHtml,
  filterState,
  isAllowedUrl,
  LOGIN_VARS,
  loginCase,
  maskAria,
  newSignals,
  normaliseConsole,
  onLoginRoute,
  parseProfile,
  resolveRoute,
  sanitizeRoute,
  saveLoginState,
  savedLoginExpiry,
  splitRunCommand,
  unnamedCounts,
  validRoute,
} from '../../src/whygraph/assets/claude-code/skills/whygraph-ui-check/capture.mjs';

const SCRIPT = fileURLToPath(
  new URL(
    '../../src/whygraph/assets/claude-code/skills/whygraph-ui-check/capture.mjs',
    import.meta.url,
  ),
);

test('isAllowedUrl accepts the three loopback forms and an allowed host', () => {
  assert.equal(isAllowedUrl('http://localhost:5173/', []), true);
  assert.equal(isAllowedUrl('http://127.0.0.1:8765/p/demo', []), true);
  assert.equal(isAllowedUrl('http://[::1]:3000/', []), true);
  assert.equal(isAllowedUrl('https://localhost/', []), true);
  assert.equal(isAllowedUrl('http://dev.example.test:3000/x', ['dev.example.test']), true);
});

test('isAllowedUrl refuses look-alike hosts, userinfo and other schemes', () => {
  assert.equal(isAllowedUrl('http://127.0.0.1.evil', []), false);
  assert.equal(isAllowedUrl('http://localhost@evil', []), false);
  assert.equal(isAllowedUrl('http://user:pw@localhost/', []), false);
  assert.equal(isAllowedUrl('file:///etc/passwd', []), false);
  assert.equal(isAllowedUrl('ftp://localhost/', []), false);
  assert.equal(isAllowedUrl('http://evil.example/', []), false);
  assert.equal(isAllowedUrl('http://evil.example/', ['dev.example.test']), false);
  assert.equal(isAllowedUrl('not a url', []), false);
});

test('validRoute follows the route pattern and refuses a protocol-relative path', () => {
  for (const ok of ['/', '/settings', '/p/demo-1/x_y.z~%20']) {
    assert.equal(validRoute(ok), true, ok);
  }
  for (const bad of ['/a b', "/a'b", '/a;b', 'settings', '//evil/x', '', '/a?b=1']) {
    assert.equal(validRoute(bad), false, bad);
  }
});

test('resolveRoute stays on the base origin and refuses //evil/x', () => {
  assert.equal(
    String(resolveRoute('/settings', 'http://localhost:5173')),
    'http://localhost:5173/settings',
  );
  assert.throws(() => resolveRoute('//evil/x', 'http://localhost:5173'));
});

test('sanitizeRoute is injective and only emits safe file-name characters', () => {
  const routes = ['/', '/a-b', '/a/b', '/a b', '/a?b:c/d', '/p/demo/explorer', '/a_b'];
  const names = routes.map((route) => sanitizeRoute(route));
  assert.equal(new Set(names).size, routes.length, names.join(' '));
  for (const name of names) {
    assert.match(name, /^[A-Za-z0-9._-]+$/);
    assert.doesNotMatch(name, /[?:/]/);
  }
  assert.match(sanitizeRoute('/'), /^root-[0-9a-f]{8}$/);
  assert.match(sanitizeRoute('/p/demo/explorer'), /^p-demo-explorer-[0-9a-f]{8}$/);
  // The 8-hex suffix is the front of a SHA-1: a sanity check on the shape only.
  assert.equal(createHash('sha1').update('x').digest('hex').length, 40);
});

test('escapeHtml escapes angle brackets, ampersand and both quotes', () => {
  assert.equal(escapeHtml('<'), '&lt;');
  assert.equal(escapeHtml('>'), '&gt;');
  assert.equal(escapeHtml('&'), '&amp;');
  assert.equal(escapeHtml('"'), '&quot;');
  assert.match(escapeHtml("'"), /^&#(?:39|x27);$/i);
  assert.equal(escapeHtml('&lt;'), '&amp;lt;');
  const out = escapeHtml(`<img src="x" onerror='y'>&`);
  assert.doesNotMatch(out, /[<>"']/);
});

test('maskAria replaces every match of every pattern', () => {
  const patterns = ['\\d{2}:\\d{2}', 'Updated .* ago'];
  const a = '- text: Clock 12:34\n- text: Updated 5 minutes ago\n- button "Save"';
  const b = '- text: Clock 09:58\n- text: Updated 2 hours ago\n- button "Save"';
  const masked = maskAria(a, patterns);
  assert.doesNotMatch(masked, /12:34/);
  assert.doesNotMatch(masked, /5 minutes ago/);
  assert.match(masked, /button "Save"/);
  assert.equal(masked, maskAria(b, patterns));
  assert.equal(maskAria(a, []), a);
});

test('unnamedCounts counts unnamed interactive elements per role', () => {
  const snapshot = [
    '- main:',
    '  - heading "Projects" [level=1]',
    '  - button "Save"',
    '  - button:',
    '    - img',
    '  - link:',
    '    - /url: /x',
    '  - link "Home":',
    '    - /url: /',
    '  - textbox',
    '  - button [disabled]:',
    '  - checkbox "Agree" [checked]',
  ].join('\n');
  assert.deepEqual(unnamedCounts(snapshot), { button: 2, link: 1, textbox: 1 });
  assert.deepEqual(unnamedCounts('- main:\n  - button "Save"'), {});
});

test('normaliseConsole drops line, column, query and hash noise', () => {
  assert.equal(normaliseConsole('x (app.js:12:3)'), normaliseConsole('x (app.js:40:9)'));
  assert.equal(
    normaliseConsole('boom (app-3f2a9c1b.js:1:2)'),
    normaliseConsole('boom (app-aa11bb22.js:5:6)'),
  );
  assert.equal(
    normaliseConsole('Failed to load http://localhost:5173/a.js?v=1#x'),
    normaliseConsole('Failed to load http://localhost:5173/a.js?v=2#y'),
  );
  assert.notEqual(normaliseConsole('x (app.js:1:1)'), normaliseConsole('y (app.js:1:1)'));
});

const empty = () => ({ console_errors: [], failed_requests: [], overflow: false, unnamed: {} });

test('newSignals ignores a pre-existing signal and reports a new one', () => {
  const before = { ...empty(), console_errors: ['A'] };
  assert.deepEqual(newSignals(before, { ...before }), []);

  const added = newSignals(before, { ...empty(), console_errors: ['A', 'B'] });
  assert.equal(added.length, 1);
  assert.match(JSON.stringify(added), /B/);
});

test('newSignals reports a failed request, overflow and a rise in unnamed controls', () => {
  const request = { method: 'GET', path: '/api/x', status: 500 };
  const failed = newSignals(empty(), { ...empty(), failed_requests: [request] });
  assert.equal(failed.length, 1);
  assert.match(JSON.stringify(failed), /\/api\/x/);
  assert.match(JSON.stringify(failed), /500/);
  assert.deepEqual(
    newSignals({ ...empty(), failed_requests: [request] }, { ...empty(), failed_requests: [request] }),
    [],
  );

  const overflow = newSignals(empty(), { ...empty(), overflow: true });
  assert.equal(overflow.length, 1);
  assert.match(JSON.stringify(overflow), /overflow/i);
  assert.deepEqual(newSignals({ ...empty(), overflow: true }, { ...empty(), overflow: true }), []);

  const rise = newSignals({ ...empty(), unnamed: { button: 1 } }, { ...empty(), unnamed: { button: 2 } });
  assert.equal(rise.length, 1);
  assert.match(JSON.stringify(rise), /button/);
  assert.deepEqual(
    newSignals({ ...empty(), unnamed: { button: 2 } }, { ...empty(), unnamed: { button: 1 } }),
    [],
  );
});

test('parseProfile reads the documented profile', () => {
  const text = [
    '---',
    'fingerprint: 3f2a9c1d',
    'derived_from: package.json; package-lock.json; vite.config.ts',
    'generated: 2026-11-06',
    '---',
    'base_url: http://localhost:5173',
    'start: npm run dev',
    'start_dir: .',
    'fixture: none',
    'wait_for: none',
    'auth: none',
    'tests: playwright e2e/',
    'stack: React 19, Tailwind 4, shadcn',
    'theme: prefers-color-scheme',
    'frontend_paths: .',
    'allow_hosts:',
    'mask: \\d{2}:\\d{2}; Updated .* ago',
    'ignore_console: \\[vite\\]; Download the React DevTools',
    'ignore_requests: /favicon.ico',
    'conventions: CLAUDE.md',
    'capture: available',
    '',
  ].join('\n');
  const profile = parseProfile(text);
  assert.equal(profile.fingerprint, '3f2a9c1d');
  assert.deepEqual(profile.derived_from, ['package.json', 'package-lock.json', 'vite.config.ts']);
  assert.equal(profile.base_url, 'http://localhost:5173');
  assert.equal(profile.start, 'npm run dev');
  assert.equal(profile.start_dir, '.');
  assert.equal(profile.fixture, 'none');
  assert.equal(profile.theme, 'prefers-color-scheme');
  assert.deepEqual(profile.frontend_paths, ['.']);
  assert.deepEqual(profile.allow_hosts, []);
  assert.deepEqual(profile.mask, ['\\d{2}:\\d{2}', 'Updated .* ago']);
  assert.deepEqual(profile.ignore_console, ['\\[vite\\]', 'Download the React DevTools']);
  assert.deepEqual(profile.ignore_requests, ['/favicon.ico']);
  assert.equal(profile.capture, 'available');
});

test('the main guard holds when the script runs through a symlink', () => {
  const dir = mkdtempSync(join(tmpdir(), 'whygraph-capture-'));
  const link = join(dir, 'capture-link.mjs');
  symlinkSync(SCRIPT, link);
  // No .whygraph/profiles/ui.md in `dir`: the probe reports "unavailable" (exit 3).
  const run = spawnSync(process.execPath, [link, '--probe'], {
    cwd: dir,
    encoding: 'utf8',
    timeout: 30000,
  });
  assert.ok([0, 3].includes(run.status), `exit ${run.status}: ${run.stderr}`);
  assert.match(run.stdout, /^capture: (?:available|unavailable)/m);
});


// ---- plan 56 section 5.12: the --e2e runner mode -----------------------------

test('childEnv drops the login and strip-list variables and sets npm_config_yes', () => {
  const env = {
    PATH: '/bin',
    WHYGRAPH_UI_USER: 'u',
    WHYGRAPH_UI_PASSWORD: 'p',
    OPENAI_API_KEY: 'k',
    AWS_SESSION_TOKEN: 't',
    CLAUDE_CODE_OAUTH_TOKEN: 'o',
    WHYGRAPH_TEST: 'w',
    GITHUB_TOKEN: 'g',
  };
  assert.deepEqual(childEnv(env), { PATH: '/bin', GITHUB_TOKEN: 'g', npm_config_yes: 'false' });
  const kept = childEnv({ ...env, WHYGRAPH_KEEP_SECRETS: '1' });
  assert.equal(kept.OPENAI_API_KEY, 'k');
  assert.equal(kept.WHYGRAPH_UI_USER, undefined);
  assert.equal(kept.WHYGRAPH_UI_PASSWORD, undefined);
});

test('splitRunCommand splits on spaces and refuses a quote, a $ or a backtick', () => {
  assert.deepEqual(splitRunCommand('npx cypress run --spec'), ['npx', 'cypress', 'run', '--spec']);
  for (const bad of ['none', '', 'npx "cypress"', "npx 'x'", 'npx $X', 'npx `x`']) {
    assert.equal(splitRunCommand(bad), null, bad);
  }
});

test('e2eDeadline reads the Verify budget and caps it at 500 s', () => {
  assert.equal(e2eDeadline('30'), 30);
  assert.equal(e2eDeadline('580'), 500);
  assert.equal(e2eDeadline(undefined), 500);
  assert.equal(e2eDeadline('x'), 500);
});

function shotsRoot() {
  const root = mkdtempSync(join(tmpdir(), 'shots-'));
  mkdirSync(join(root, 'shots', 'login.cy.js'), { recursive: true });
  mkdirSync(join(root, 'shots', 'cart.cy.js'), { recursive: true });
  return root;
}

test('copyShots copies newer images, walks nested folders and avoids collisions', () => {
  const root = shotsRoot();
  writeFileSync(join(root, 'shots', 'login.cy.js', 'a.png'), 'x');
  writeFileSync(join(root, 'shots', 'cart.cy.js', 'a.png'), 'y');
  writeFileSync(join(root, 'shots', 'notes.txt'), 'z');
  const old = join(root, 'shots', 'old.png');
  writeFileSync(old, 'o');
  utimesSync(old, new Date(1000), new Date(1000));
  const out = join(root, 'out');
  const res = copyShots(root, 'shots', out, Date.now() - 60000);
  assert.deepEqual(res, { copied: 2, refused: null });
  assert.deepEqual(readdirSync(join(out, 'shots')).sort(), ['cart.cy.js-a.png', 'login.cy.js-a.png']);
});

test('copyShots skips a symlinked file and refuses a symlinked folder', () => {
  const root = shotsRoot();
  const outside = mkdtempSync(join(tmpdir(), 'outside-'));
  writeFileSync(join(outside, 'secret.png'), 's');
  symlinkSync(join(outside, 'secret.png'), join(root, 'shots', 'x.png'));
  symlinkSync(outside, join(root, 'shots', 'linked'));
  const res = copyShots(root, 'shots', join(root, 'out'), 0);
  assert.deepEqual(res, { copied: 0, refused: null });
  symlinkSync(join(root, 'shots'), join(root, 'via-link'));
  assert.equal(copyShots(root, 'via-link', join(root, 'out'), 0).refused, 'e2e_shots goes through a symlink');
  assert.equal(copyShots(root, '../x', join(root, 'out'), 0).refused, 'e2e_shots is not a plain relative path');
  assert.deepEqual(copyShots(root, 'missing', join(root, 'out'), 0), { copied: 0, refused: null });
});

test('copyShots stops at 40 files', () => {
  const root = shotsRoot();
  for (let i = 0; i < 45; i += 1) writeFileSync(join(root, 'shots', `s${String(i).padStart(2, '0')}.png`), 'x');
  assert.equal(copyShots(root, 'shots', join(root, 'out'), 0).copied, 40);
});

async function e2eRepo(body, extra = '') {
  const server = createServer((q, s) => s.end('ok'));
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const root = mkdtempSync(join(tmpdir(), 'e2e-'));
  mkdirSync(join(root, '.whygraph', 'profiles'), { recursive: true });
  mkdirSync(join(root, 'e2e'));
  writeFileSync(
    join(root, '.whygraph', 'profiles', 'ui.md'),
    `base_url: http://127.0.0.1:${server.address().port}/\nstart: none\ne2e_run: node --test\ne2e_dir: e2e\ne2e_shots: none\n${extra}`,
  );
  writeFileSync(join(root, 'e2e', 'a.test.mjs'), body);
  return { root, server };
}

function runE2e(root, env = {}) {
  return new Promise((done) => {
    const child = spawn(process.execPath, [SCRIPT, '--e2e', 'e2e/a.test.mjs', '--out', 'out'], {
      cwd: root,
      // The outer test runner's context would make the inner node --test
      // report to it instead of setting its own exit code.
      env: Object.fromEntries(
        Object.entries({ ...process.env, ...env }).filter(([k]) => k !== 'NODE_TEST_CONTEXT'),
      ),
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    child.on('exit', (code) => done({ code, stdout, stderr }));
  });
}

test('--e2e exits 0 for a passing test and 2 for a failing one', async () => {
  const pass = await e2eRepo("import {test} from 'node:test';\ntest('ok', () => {});\n");
  const run = await runE2e(pass.root);
  pass.server.close();
  assert.equal(run.code, 0, run.stdout + run.stderr);
  const fail = await e2eRepo("import {test} from 'node:test';\nimport a from 'node:assert';\ntest('no', () => a.equal(1, 2));\n");
  const red = await runE2e(fail.root);
  fail.server.close();
  assert.equal(red.code, 2, red.stdout + red.stderr);
  assert.match(red.stdout.trim().split('\n').at(-1), /^log: out\/e2e\.log$/);
});

test('--e2e exits 5 on the deadline and passes no secret to the runner', async () => {
  const slow = await e2eRepo(
    "import {test} from 'node:test';\nimport {writeFileSync} from 'node:fs';\n" +
      "test('slow', async () => { writeFileSync('env.json', JSON.stringify(process.env)); await new Promise((r) => setTimeout(r, 20000)); });\n",
  );
  const run = await runE2e(slow.root, { WHYGRAPH_VERIFY_TIMEOUT: '3', WHYGRAPH_UI_PASSWORD: 'pw', OPENAI_API_KEY: 'k' });
  slow.server.close();
  assert.equal(run.code, 5, run.stdout + run.stderr);
  assert.match(run.stderr, /timed out after 3 s/);
  const seen = JSON.parse(readFileSync(join(slow.root, 'env.json'), 'utf8'));
  assert.equal(seen.WHYGRAPH_UI_PASSWORD, undefined);
  assert.equal(seen.OPENAI_API_KEY, undefined);
  assert.equal(seen.npm_config_yes, 'false');
});

test('--e2e refuses a quoted e2e_run, a file outside e2e_dir and a remote base', async () => {
  const quoted = await e2eRepo('', '');
  writeFileSync(
    join(quoted.root, '.whygraph', 'profiles', 'ui.md'),
    "base_url: http://127.0.0.1:1/\ne2e_run: node '--test'\ne2e_dir: e2e\n",
  );
  quoted.server.close();
  assert.equal((await runE2e(quoted.root)).code, 3);
  const remote = await e2eRepo('', '');
  writeFileSync(
    join(remote.root, '.whygraph', 'profiles', 'ui.md'),
    'base_url: http://dev.example.test/\nallow_hosts: dev.example.test\ne2e_run: node --test\ne2e_dir: e2e\n',
  );
  remote.server.close();
  assert.equal((await runE2e(remote.root)).code, 3);
});

test('parseProfile drops a trailing note and backticks from a value', () => {
  const p = parseProfile(
    'base_url: http://127.0.0.1:5195 (`vite.config.js` sets port 5195)\n' +
      'start: `npm run dev` (runs `vite`)\n' +
      'e2e_run: npx cypress run --spec\n' +
      'fixture: `none` as a command. Fixtures are the flags in `src/flags.js`.\n',
  );
  assert.equal(p.fixture, 'none');
  assert.equal(p.base_url, 'http://127.0.0.1:5195');
  assert.equal(p.start, 'npm run dev');
  assert.equal(p.e2e_run, 'npx cypress run --spec');
});

// ---- plan 56 section 5.16: log in, or use a saved login ----------------------
//
// The browser cases use the playground's Playwright (src/playground/node_modules)
// and skip when it is not installed or chromium cannot launch.

const REPO = fileURLToPath(new URL('../..', import.meta.url));
const PW_MODULES = join(REPO, 'src', 'playground', 'node_modules');
const PASSWORD = 'Pw-7c1f-do-not-print';
const USER = 'dev-user-41d2';
const LOGIN_ENV = { WHYGRAPH_UI_USER: USER, WHYGRAPH_UI_PASSWORD: PASSWORD };
const AUTH_FILE = join('.whygraph', 'profiles', 'ui-auth.json');

async function playwrightState() {
  try {
    const req = createRequire(join(PW_MODULES, 'x.js'));
    const { chromium } = req('playwright');
    if (!existsSync(chromium.executablePath())) return { installed: false, launches: false };
    try {
      const browser = await chromium.launch({ headless: true });
      await browser.close();
      return { installed: true, launches: true };
    } catch {
      return { installed: true, launches: false };
    }
  } catch {
    return { installed: false, launches: false };
  }
}

const PW = await playwrightState();
const NO_PW = PW.installed ? false : 'the playground Playwright or its chromium is not installed';
const NO_BROWSER = PW.launches ? false : 'chromium from the playground Playwright cannot launch';

function profileText(base, extra = '') {
  return [
    `base_url: ${base}`,
    'start: none',
    'theme: none',
    'frontend_paths: .',
    'ignore_requests: /favicon.ico',
    'auth: required',
    'login_route: `/login`',
    'login_fields: `#user`; `#pass`; `button[type=submit]`',
    extra,
    '',
  ].join('\n');
}

function loginRepo(base, { extra = '', playwright = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'login-'));
  mkdirSync(join(root, '.whygraph', 'profiles'), { recursive: true });
  writeFileSync(join(root, '.whygraph', 'profiles', 'ui.md'), profileText(base, extra));
  if (playwright) symlinkSync(PW_MODULES, join(root, 'node_modules'));
  return root;
}

function runScript(root, args, env = {}) {
  return new Promise((done) => {
    const clean = Object.fromEntries(
      Object.entries({ ...process.env, ...env }).filter(
        ([k]) => k !== 'NODE_TEST_CONTEXT' && (!k.startsWith('WHYGRAPH_UI_') || k in env),
      ),
    );
    const child = spawn(process.execPath, [SCRIPT, ...args], { cwd: root, env: clean });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    child.on('exit', (code) => done({ code, stdout, stderr }));
  });
}

function allFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    const st = lstatSync(path);
    if (st.isSymbolicLink()) continue;
    if (st.isDirectory()) out.push(...allFiles(path));
    else out.push(path);
  }
  return out;
}

function assertNoSecret(root, ...texts) {
  for (const text of texts) {
    assert.ok(!text.includes(PASSWORD), `the password leaked: ${text}`);
    assert.ok(!text.includes(USER), `the user name leaked: ${text}`);
  }
  for (const file of allFiles(root)) {
    const body = readFileSync(file).toString('latin1');
    assert.ok(!body.includes(PASSWORD), `the password is in ${file}`);
    assert.ok(!body.includes(USER), `the user name is in ${file}`);
  }
}

function cookie(name, value, domain, expires) {
  return { name, value, domain, path: '/', expires, httpOnly: true, secure: false, sameSite: 'Lax' };
}

function writeState(root, state) {
  writeFileSync(join(root, AUTH_FILE), JSON.stringify(state));
}

const LATER = Math.floor(Date.now() / 1000) + 7200;
const EARLIER = Math.floor(Date.now() / 1000) - 7200;

// A local app with a login form: POST /login with the right user and password
// sets sid=good and redirects to /; every other page redirects to /login
// without it. The login page also asks another origin for a beacon.
async function loginApp({ otherPort = 0, slowLogin = false } = {}) {
  const posted = [];
  const server = createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    const authed = /(?:^|; )sid=good(?:;|$)/.test(req.headers.cookie || '');
    if (url.pathname === '/login' && req.method === 'POST') {
      let body = '';
      req.on('data', (d) => (body += d));
      req.on('end', () => {
        const form = new URLSearchParams(body);
        posted.push({ user: form.get('user'), pass: form.get('pass') });
        const ok = !slowLogin && form.get('user') === USER && form.get('pass') === PASSWORD;
        const signedIn = { 'Set-Cookie': 'sid=good; Path=/; Max-Age=3600; HttpOnly', Location: '/' };
        res.writeHead(302, ok ? signedIn : { Location: '/login' });
        res.end();
      });
      return;
    }
    if (url.pathname === '/login') {
      const beacon = otherPort
        ? `<img src="http://127.0.0.1:${otherPort}/pixel.png" alt="">` +
          `<script>fetch('http://127.0.0.1:${otherPort}/beacon').catch(() => {})</script>`
        : '';
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end(
        `<!doctype html><title>Sign in</title><main><h1>Sign in page</h1>${beacon}<form method="post" action="/login">` +
          '<label>User <input id="user" name="user"></label><label>Password <input id="pass" name="pass" type="password"></label>' +
          '<button type="submit">Sign in</button></form></main>',
      );
      return;
    }
    if (url.pathname === '/favicon.ico') {
      res.writeHead(404);
      res.end();
      return;
    }
    if (!authed) {
      res.writeHead(302, { Location: '/login' });
      res.end();
      return;
    }
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end('<!doctype html><title>Home</title><main><h1>Dashboard home</h1></main>');
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { server, posted, base: `http://127.0.0.1:${server.address().port}/` };
}

async function otherOrigin() {
  const hits = [];
  const server = createServer((req, res) => {
    hits.push(req.url);
    res.end('x');
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { server, hits, port: server.address().port };
}

test('the password is absent from every output (no browser)', async () => {
  // Every exit path that needs no browser, with both variables set.
  const remote = loginRepo('http://dev.example.test/', { extra: 'allow_hosts: dev.example.test' });
  const local = loginRepo('http://127.0.0.1:1/');
  const runs = [
    await runScript(remote, ['--probe'], LOGIN_ENV),
    await runScript(remote, ['--tag', 'before', '--out', 'out', '/'], LOGIN_ENV),
    await runScript(remote, ['--login'], LOGIN_ENV),
    await runScript(local, ['--probe'], LOGIN_ENV),
    await runScript(local, ['--tag', 'before', '--out', 'out', '/'], LOGIN_ENV),
    await runScript(local, ['--tag', 'after', '--out', 'out', '/'], LOGIN_ENV),
  ];
  for (const run of runs) assertNoSecret(remote, run.stdout, run.stderr);
  assertNoSecret(local);
  assert.ok(!existsSync(join(local, AUTH_FILE)));
});

test('the password is absent from every output (form login)', { skip: NO_BROWSER, timeout: 120000 }, async () => {
  const app = await loginApp();
  const root = loginRepo(app.base, { playwright: true });
  const run = await runScript(root, ['--tag', 'before', '--out', 'out', '/'], LOGIN_ENV);
  const after = await runScript(root, ['--tag', 'after', '--out', 'out', '/'], LOGIN_ENV);
  app.server.close();
  assert.equal(run.code, 0, run.stdout + run.stderr);
  assert.equal(after.code, 0, after.stdout + after.stderr);
  // The fill typed the password into the form, and only there.
  assert.ok(app.posted.length >= 2 && app.posted.every((p) => p.pass === PASSWORD && p.user === USER));
  assertNoSecret(root, run.stdout, run.stderr, after.stdout, after.stderr);
  assert.ok(!existsSync(join(root, AUTH_FILE)), 'a form login writes no state file');
  // The captured page is the app behind the login, never the login page.
  for (const tag of ['before', 'after']) {
    const arias = readdirSync(join(root, 'out', tag)).filter((n) => n.endsWith('.aria.txt'));
    assert.ok(arias.length > 0, tag);
    for (const name of arias) {
      const text = readFileSync(join(root, 'out', tag, name), 'utf8');
      assert.match(text, /Dashboard home/);
      assert.doesNotMatch(text, /Sign in page/);
    }
  }
  assert.ok(!readFileSync(join(root, 'out', 'signals.json'), 'utf8').includes('/login'));
});

test('a failed form login prints one line and nothing else', { skip: NO_BROWSER, timeout: 120000 }, async () => {
  const app = await loginApp({ slowLogin: true });
  const root = loginRepo(app.base, { playwright: true });
  const began = Date.now();
  const run = await runScript(root, ['--tag', 'before', '--out', 'out', '/'], LOGIN_ENV);
  app.server.close();
  assert.equal(run.code, 3, run.stdout + run.stderr);
  assert.equal(run.stderr, 'login failed: still on /login\n');
  assert.ok(Date.now() - began >= 14000, 'it waits for the form to be left');
  assert.ok(Date.now() - began < 60000);
  assertNoSecret(root, run.stdout, run.stderr);
  const shots = existsSync(join(root, 'out', 'before')) ? readdirSync(join(root, 'out', 'before')) : [];
  assert.deepEqual(shots, [], 'the login page is never captured');
});

test('the form login aborts every request to another origin', { skip: NO_BROWSER, timeout: 120000 }, async () => {
  const other = await otherOrigin();
  const app = await loginApp({ otherPort: other.port });
  const root = loginRepo(app.base, { playwright: true });
  const run = await runScript(root, ['--tag', 'before', '--out', 'out', '/'], LOGIN_ENV);
  app.server.close();
  other.server.close();
  assert.equal(run.code, 0, run.stdout + run.stderr);
  assert.deepEqual(other.hits, []);
});

test('the form login wins over a saved login', { skip: NO_BROWSER, timeout: 120000 }, async () => {
  const app = await loginApp();
  const root = loginRepo(app.base, { playwright: true });
  // A saved login that the app no longer accepts: only the form login succeeds.
  writeState(root, { cookies: [cookie('sid', 'stale', '127.0.0.1', LATER)], origins: [] });
  const run = await runScript(root, ['--tag', 'before', '--out', 'out', '/'], LOGIN_ENV);
  app.server.close();
  assert.equal(run.code, 0, run.stdout + run.stderr);
  assert.ok(app.posted.length >= 1);
});

test('a saved login captures, and an expired one exits 3 with the --login hint', { skip: NO_BROWSER, timeout: 120000 }, async () => {
  const app = await loginApp();
  const root = loginRepo(app.base, { playwright: true });
  writeState(root, { cookies: [cookie('sid', 'good', '127.0.0.1', LATER)], origins: [] });
  const live = await runScript(root, ['--tag', 'before', '--out', 'out', '/']);
  assert.equal(live.code, 0, live.stdout + live.stderr);
  assert.match(readdirSync(join(root, 'out', 'before')).join(' '), /\.png/);
  writeState(root, { cookies: [cookie('sid', 'stale', '127.0.0.1', LATER)], origins: [] });
  const gone = await runScript(root, ['--tag', 'after', '--out', 'out', '/']);
  app.server.close();
  assert.equal(gone.code, 3, gone.stdout + gone.stderr);
  assert.match(gone.stderr, /^the saved login expired: run node \S*capture\.mjs --login\n$/);
  assert.equal(app.posted.length, 0);
  assert.deepEqual(readdirSync(join(root, 'out', 'after')), [], 'the login page is never captured');
});

test('loginCase resolves the four cases in order', () => {
  const base = 'http://127.0.0.1:5173/';
  const p = parseProfile(profileText(base));
  const none = parseProfile(profileText(base).replace('auth: required', 'auth: none'));
  assert.deepEqual(loginCase(none, LOGIN_ENV, true, 's.mjs'), { kind: 'none' });
  assert.deepEqual(loginCase(p, LOGIN_ENV, false, 's.mjs'), { kind: 'form' });
  assert.deepEqual(loginCase(p, LOGIN_ENV, true, 's.mjs'), { kind: 'form' }, 'the form login wins');
  assert.deepEqual(loginCase(p, {}, true, 's.mjs'), { kind: 'saved' });
  assert.deepEqual(loginCase(p, { WHYGRAPH_UI_USER: USER }, true, 's.mjs'), { kind: 'saved' });
  const notSetUp = {
    kind: 'unavailable',
    reason: 'login not set up: export WHYGRAPH_UI_USER and WHYGRAPH_UI_PASSWORD, or run node s.mjs --login',
  };
  assert.deepEqual(loginCase(p, {}, false, 's.mjs'), notSetUp);
  assert.deepEqual(loginCase(p, { WHYGRAPH_UI_USER: USER, WHYGRAPH_UI_PASSWORD: '' }, false, 's.mjs'), notSetUp);
  // Without a login route or three fields there is no form login.
  const noFields = parseProfile(profileText(base).replace(/^login_fields: .*$/m, 'login_fields: `#user`; `#pass`'));
  assert.deepEqual(loginCase(noFields, LOGIN_ENV, false, 's.mjs'), notSetUp);
  assert.deepEqual(loginCase(noFields, LOGIN_ENV, true, 's.mjs'), { kind: 'saved' });
  const noRoute = parseProfile(profileText(base).replace(/^login_route: .*$/m, 'login_route: none'));
  assert.deepEqual(loginCase(noRoute, LOGIN_ENV, false, 's.mjs'), notSetUp);
});

test('loginCase reads only the two fixed variable names', () => {
  const p = parseProfile(
    profileText('http://127.0.0.1:5173/', 'user_var: MY_USER\npassword_var: MY_SECRET\nlogin_user_env: MY_USER'),
  );
  const env = { MY_USER: 'x', MY_SECRET: 'y', WHYGRAPH_UI_USER_X: 'z' };
  assert.equal(loginCase(p, env, false, 's.mjs').kind, 'unavailable');
  assert.deepEqual(LOGIN_VARS, ['WHYGRAPH_UI_USER', 'WHYGRAPH_UI_PASSWORD']);
});

test('loginCase refuses a host that is not loopback, whatever allow_hosts says', () => {
  const p = parseProfile(profileText('http://dev.example.test/', 'allow_hosts: dev.example.test'));
  for (const [env, saved] of [
    [LOGIN_ENV, false],
    [{}, true],
    [LOGIN_ENV, true],
  ]) {
    const c = loginCase(p, env, saved, 's.mjs');
    assert.equal(c.kind, 'unavailable');
    assert.match(c.reason, /loopback/);
  }
});

test('parseProfile reads login_route and the three login_fields', () => {
  const p = parseProfile(profileText('http://127.0.0.1:5173/'));
  assert.equal(p.login_route, '/login');
  assert.deepEqual(p.login_fields, ['#user', '#pass', 'button[type=submit]']);
  const plain = parseProfile('login_fields: input[name=email]; input[name=password]; form button\n');
  assert.deepEqual(plain.login_fields, ['input[name=email]', 'input[name=password]', 'form button']);
});

test('onLoginRoute compares the path only', () => {
  assert.equal(onLoginRoute('http://127.0.0.1:1/login', '/login'), true);
  assert.equal(onLoginRoute('http://127.0.0.1:1/login/?next=/x#y', '/login'), true);
  assert.equal(onLoginRoute('http://127.0.0.1:1/', '/login'), false);
  assert.equal(onLoginRoute('http://127.0.0.1:1/login-help', '/login'), false);
  assert.equal(onLoginRoute('not a url', '/login'), false);
});

test('filterState keeps only the base host cookies and the base origin storage', () => {
  const state = {
    cookies: [
      cookie('sid', 'a', '127.0.0.1', LATER),
      cookie('dot', 'b', '.127.0.0.1', LATER),
      cookie('idp', 'c', 'login.example.test', LATER),
      cookie('lh', 'd', 'localhost', LATER),
      cookie('sub', 'e', '.example.test', LATER),
    ],
    origins: [
      { origin: 'http://127.0.0.1:5173', localStorage: [{ name: 't', value: '1' }], indexedDB: [{ name: 'db' }] },
      { origin: 'http://127.0.0.1:9999', localStorage: [{ name: 'o', value: '2' }] },
      { origin: 'https://login.example.test', localStorage: [{ name: 'i', value: '3' }] },
    ],
  };
  const kept = filterState(state, 'http://127.0.0.1:5173/');
  assert.deepEqual(
    kept.cookies.map((c) => c.name),
    ['sid', 'dot'],
  );
  assert.deepEqual(kept.origins, [{ origin: 'http://127.0.0.1:5173', localStorage: [{ name: 't', value: '1' }] }]);
  assert.doesNotMatch(JSON.stringify(kept), /example\.test|9999/);
  assert.deepEqual(filterState({}, 'http://127.0.0.1:5173/'), { cookies: [], origins: [] });
  assert.deepEqual(filterState(null, 'http://127.0.0.1:5173/'), { cookies: [], origins: [] });
  const v6 = filterState({ cookies: [cookie('s', 'x', '::1', LATER), cookie('t', 'y', '[::1]', LATER)] }, 'http://[::1]:3000/');
  assert.equal(v6.cookies.length, 2);
});

test('savedLoginExpiry reads the base host cookies only', () => {
  const base = 'http://127.0.0.1:5173/';
  const now = Date.now();
  const live = savedLoginExpiry({ cookies: [cookie('a', '1', '127.0.0.1', EARLIER), cookie('b', '2', '127.0.0.1', LATER)] }, base, now);
  assert.equal(live.expired, false);
  assert.equal(live.expires.getTime(), LATER * 1000);
  const past = savedLoginExpiry(
    { cookies: [cookie('a', '1', '127.0.0.1', EARLIER), cookie('idp', '2', 'login.example.test', LATER)] },
    base,
    now,
  );
  assert.deepEqual(past, { expired: true, expires: null });
  const session = savedLoginExpiry({ cookies: [cookie('a', '1', '127.0.0.1', -1)] }, base, now);
  assert.deepEqual(session, { expired: false, expires: null });
  assert.deepEqual(savedLoginExpiry({ cookies: [] }, base, now), { expired: false, expires: null });
});

test('saveLoginState writes a filtered 0600 file and refuses a symlink', () => {
  const dir = mkdtempSync(join(tmpdir(), 'save-'));
  const path = join(dir, 'ui-auth.json');
  saveLoginState(path, { cookies: [cookie('sid', 'a', '127.0.0.1', LATER), cookie('x', 'b', 'evil.test', LATER)] }, 'http://127.0.0.1:1/');
  assert.equal(lstatSync(path).mode & 0o777, 0o600);
  const saved = JSON.parse(readFileSync(path, 'utf8'));
  assert.deepEqual(
    saved.cookies.map((c) => c.name),
    ['sid'],
  );
  saveLoginState(path, { cookies: [] }, 'http://127.0.0.1:1/');
  assert.equal(lstatSync(path).mode & 0o777, 0o600);
  assert.deepEqual(readdirSync(dir), ['ui-auth.json'], 'no temporary file is left');
  const target = join(dir, 'target.json');
  writeFileSync(target, 'keep');
  const link = join(dir, 'link.json');
  symlinkSync(target, link);
  assert.throws(() => saveLoginState(link, { cookies: [] }, 'http://127.0.0.1:1/'), /symlink/);
  assert.equal(readFileSync(target, 'utf8'), 'keep');
});

test('--probe resolves the login case without a browser', async () => {
  const notSetUp = loginRepo('http://127.0.0.1:1/');
  const a = await runScript(notSetUp, ['--probe']);
  assert.equal(a.code, 3);
  assert.match(
    a.stdout,
    /^capture: unavailable: login not set up: export WHYGRAPH_UI_USER and WHYGRAPH_UI_PASSWORD, or run node \S*capture\.mjs --login$/m,
  );
  const expired = loginRepo('http://127.0.0.1:1/');
  writeState(expired, { cookies: [cookie('sid', 'a', '127.0.0.1', EARLIER), cookie('idp', 'b', 'login.example.test', LATER)] });
  const b = await runScript(expired, ['--probe']);
  assert.equal(b.code, 3);
  assert.equal(b.stdout, 'capture: unavailable: the saved login expired\n');
  const remote = loginRepo('http://dev.example.test/', { extra: 'allow_hosts: dev.example.test' });
  const c = await runScript(remote, ['--probe'], LOGIN_ENV);
  assert.equal(c.code, 3);
  assert.match(c.stdout, /^capture: unavailable: .*loopback/);
});

test('--probe prints the expiry of a live saved login', { skip: NO_PW }, async () => {
  const root = loginRepo('http://127.0.0.1:1/', { playwright: true });
  writeState(root, { cookies: [cookie('sid', 'a', '127.0.0.1', LATER)] });
  const run = await runScript(root, ['--probe']);
  assert.equal(run.code, 0, run.stdout + run.stderr);
  const date = new Date(LATER * 1000).toISOString().replace(/\.\d{3}Z$/, 'Z');
  assert.equal(run.stdout, `capture: available (saved login expires ${date})\n`);
  const form = await runScript(root, ['--probe'], LOGIN_ENV);
  assert.equal(form.stdout, 'capture: available\n');
});

test('a symlinked or tracked saved login is refused', async () => {
  const linked = loginRepo('http://127.0.0.1:1/');
  const outside = join(mkdtempSync(join(tmpdir(), 'outside-')), 'state.json');
  writeFileSync(outside, JSON.stringify({ cookies: [cookie('sid', 'a', '127.0.0.1', LATER)] }));
  symlinkSync(outside, join(linked, AUTH_FILE));
  for (const args of [['--probe'], ['--tag', 'before', '--out', 'out', '/'], ['--login']]) {
    const run = await runScript(linked, args);
    assert.equal(run.code, 3, run.stdout + run.stderr);
    assert.match(run.stdout + run.stderr, /symlink/);
  }
  const tracked = loginRepo('http://127.0.0.1:1/');
  writeState(tracked, { cookies: [cookie('sid', 'a', '127.0.0.1', LATER)] });
  spawnSync('git', ['init', '-q'], { cwd: tracked });
  spawnSync('git', ['add', '-f', AUTH_FILE], { cwd: tracked });
  for (const args of [['--probe'], ['--tag', 'before', '--out', 'out', '/'], ['--login']]) {
    const run = await runScript(tracked, args);
    assert.equal(run.code, 3, run.stdout + run.stderr);
    assert.match(run.stdout + run.stderr, /tracked by git/);
  }
});

test('--login refuses a host that is not loopback and takes no other argument', async () => {
  const remote = loginRepo('http://dev.example.test/', { extra: 'allow_hosts: dev.example.test' });
  const run = await runScript(remote, ['--login']);
  assert.equal(run.code, 3, run.stdout + run.stderr);
  assert.match(run.stderr, /loopback/);
  const usage = await runScript(remote, ['--login', '--tag', 'before']);
  assert.equal(usage.code, 4);
  assert.ok(!existsSync(join(remote, AUTH_FILE)));
});

test('a capture without a login set up exits 3 with the setup line', async () => {
  const root = loginRepo('http://127.0.0.1:1/');
  const run = await runScript(root, ['--tag', 'before', '--out', 'out', '/']);
  assert.equal(run.code, 3, run.stdout + run.stderr);
  assert.match(
    run.stderr,
    /^login not set up: export WHYGRAPH_UI_USER and WHYGRAPH_UI_PASSWORD, or run node \S*capture\.mjs --login\n$/,
  );
});

test('--e2e passes neither login variable to the test', async () => {
  const repo = await e2eRepo(
    "import {test} from 'node:test';\nimport {writeFileSync} from 'node:fs';\n" +
      "test('env', () => { writeFileSync('env.json', JSON.stringify(process.env)); });\n",
    'auth: required\n',
  );
  const run = await runE2e(repo.root, { ...LOGIN_ENV, WHYGRAPH_KEEP_SECRETS: '1', OTHER_VAR: 'kept' });
  repo.server.close();
  assert.equal(run.code, 0, run.stdout + run.stderr);
  const seen = JSON.parse(readFileSync(join(repo.root, 'env.json'), 'utf8'));
  assert.equal(seen.WHYGRAPH_UI_USER, undefined);
  assert.equal(seen.WHYGRAPH_UI_PASSWORD, undefined);
  assert.equal(seen.OTHER_VAR, 'kept');
  assertNoSecret(repo.root, run.stdout, run.stderr);
});
