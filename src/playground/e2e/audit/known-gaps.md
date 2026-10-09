# Screenshot audit - gaps

What the audit does not capture, and why. The second half of this file is
generated per run (`global-setup.ts`).

## Known, by design

- **Phone width is a narrow desktop browser.** The phone shots resize the
  viewport to 390x844 in the same desktop context (no touch, no mobile user
  agent, `isMobile` off), so Tailwind's breakpoints apply but touch-only
  behaviour does not.
- **"Full page" means grown to the content.** The app shell is `h-screen` with
  an inner scroller, so each shot grows the viewport to the tallest scroll
  container's content (capped at 9000 px) instead of using Playwright's
  `fullPage`. Screens with `viewportOnly` (running scans, streaming chat, menus,
  dialogs) are captured at the plain viewport height.
- **Loading / error variants are synthetic.** They hold or fail the page's API
  GETs with a route intercept (everything but `/api/portal/state` and, on
  project pages, the project itself), so they show what the SPA does with a slow
  or failing API, not a real server fault. Captured for Projects, Overview,
  Explorer, Chat, Scans, settings and Usage; other pages only in their real
  states.
- **No real LLM.** Chats and usage come from `llm_script.py` (scripted tool
  calls, a chart, fixed token counts). Rationale generation gets plain text, so
  its "generated" shot shows whatever the generator makes of that.
- **The fake scanner records no commits in local mode**, so local Evidence /
  History tabs and the first-scan cost card are in their empty states
  ("Nothing to describe"). The production portal's scanner runs the real git
  crawl, so its projects have commits.
- **Production first-scan progress mid-run** is not reliably capturable: the
  production scanner's `--real-git` path ignores the control `delay` and
  finishes in a few seconds. The local wizard's running shot covers the same
  component.
- **GitHub's own pages** (the fake's authorize / install pages) are not
  WhyGraph screens and are not captured.
- **Machines tab data**: machines appear only for usage from connected local
  portals generating rationale on the platform; the audit captures the tab in
  whatever state the linked flow leaves it (usually empty).
- **Password reset with a valid link**: `/reset` is captured without a token
  only.
- **Rationale generation in local mode**: the local fake scan records no
  commits, so the Rationale tab says "no historical evidence" and Generate is
  disabled; the generated-card state is not reached in either mode.
- **Production Explorer evidence / rationale for the fixture symbols** answer
  400 (`git blame failed`): the fake scanner indexes `src/demo.py`, which the
  fake GitHub's `ben/demo` does not contain. The screens show the raw error
  message as the UI renders it, which is itself worth a look.
- **GitHub avatars** point at `avatars.example.test` (the fake), so every
  avatar request fails with `ERR_NAME_NOT_RESOLVED` and pages show the fallback
  initials; those console lines in the flagged list are harness noise.
