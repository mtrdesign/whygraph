# Screenshot audit (M2f-3)

A scripted walk through every screen of both portals, captured as full-height
PNGs in **light and dark** at **desktop (1280x900) and phone (390x844)** width,
with a manifest. It is research tooling for the M2f-3 UX pass, not a test
suite: a screen that cannot be reached is recorded in `gaps.md` and the walk
goes on.

## Run it

Needs what `make e2e` needs (Docker, `uv sync`, Node 22, `openssl`, `python3`):

```bash
source ~/.nvm/nvm.sh && nvm use 22
make audit                      # both portals; ARGS="--project=local" for a subset
```

`make audit` builds the SPA, wraps the run in `caffeinate -dimsu` when that exists
(the machine must not sleep for half an hour) and prints the output directory. It is
run by hand, before a merge - never in CI and not part of `make check`. The same thing
without make: `sh src/playground/e2e/run.sh -c e2e/audit/playwright.config.ts`.

`run.sh` starts the throwaway Postgres, both portals, the fake GitHub and the
fake LLM exactly as for `make e2e`, then runs `playwright test` with its own
`-c e2e/playwright.config.ts` followed by your arguments - and the **last `-c`
wins**, so nothing in `run.sh` changes. A subset: append `--project=local` or
`--project=production` (`linked` depends on both, so it always runs them first).

Takes about 25-35 minutes. Output goes to `$AUDIT_OUT`
(default `src/playground/e2e/.artifacts/audit/`, gitignored):

- `shots/<mode>/<area>/<page>__<state>__<theme>__<width>.png`
- `manifest.json` - one entry per PNG: mode, area, page, URL / route, state,
  theme, width, captured height, a one-line description, and, when there were
  any, the console errors, API answers >= 400 and phone-width horizontal
  overflow seen while it was on screen
- `gaps.md` - the known gaps (`known-gaps.md`) plus every step that failed in
  this run and every flagged screenshot
- `failures/` - a plain screenshot of the page at each failed step

Environment: `AUDIT_OUT` (output dir), `AUDIT_LLM_PORT` (the scripted LLM,
default 18769), plus everything `run.sh` reads (`E2E_PORT`, `E2E_CHANNEL`,
`E2E_KEEP=1`, ...).

## How it works

- `playwright.config.ts` - three projects: `local`, `production`, `linked`
  (depends on both). One worker; each project is one long test.
- `global-setup.ts` - refuses non-fresh portals (the audit walks the first-run
  screens), creates the local fixture repos (`notes`, `billing`, `web`,
  `docs-site`, `legacy`), starts `llm_script.py`, and on teardown writes
  `manifest.json` and `gaps.md`.
- `llm_script.py` - an OpenAI-compatible fake that can call tools: a message
  containing `[[chart]]` gets `search_symbols` + `run_graph_stats`, then
  `render_chart`, then a Markdown answer; `[[error]]` gets a 500; `[[slow]]`
  streams for ~20 s. Every call reports usage, so chats fill Usage & cost.
- `lib/shoot.ts` - `shoot()` (the four captures, grown to the content height,
  because the app shell is `h-screen` with an inner scroller), `variants()`
  (loading = API GETs held open, error = 500, forbidden = coded 403, all by
  route intercept), `attempt()` (a failed step becomes a gap) and the manifest /
  gap writers.
- `lib/seed.ts` - the scripted LLM config (an org price override included),
  chats, the fake scanner's `delay` / `fail` control files.
- `local.audit.ts`, `production.audit.ts`, `linked.audit.ts` - the walks.
  They reuse `../lib/*.ts` (sign-in, org creation, GitHub import).

Seeded data: five local projects (ok, failed scan, scanning, folder missing,
not initialized), scans of every outcome, chats with tool cards and a chart, a
provider error, budgets (project hard stop, org banner); on production three
orgs, two imported repos, members with different roles and grants, pending
invitations, a Restricted project, chats by two people, member / org / project
budgets at 50 / 75 / 100 %, a failed scan, a push-triggered sync, a linked
local portal (connected, then revoked), and finally a repository the GitHub
App lost access to.
