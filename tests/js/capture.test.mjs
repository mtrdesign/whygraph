// Tests for the UI capture script's pure exports (plan 45, section 9 case 6).
// Run through Node's built-in runner: `node --test tests/js/capture.test.mjs`.
// Nothing here needs Playwright or a browser: the script only loads Playwright
// inside its CLI path, behind a main guard.

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, symlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  escapeHtml,
  isAllowedUrl,
  maskAria,
  newSignals,
  normaliseConsole,
  parseProfile,
  resolveRoute,
  sanitizeRoute,
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
