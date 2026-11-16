---
name: ui-surface
description: The project's UI surface: stack, how to start it, its routes, and the states a UI task should specify.
requires:
tools: read, codegraph_search
section: UI profile
enabled: true
budget: 33
consent: false
---

*Question:* what does this project's UI look like, how do you start it, and which routes and states does this task touch?

1. If the prompt carries a `PROFILE:` block, use it as the profile. Skip to step 4.
2. Derive the profile with at most twelve reads. Report each item as a `key: value` line. Write the value alone, with no note and no backticks: the capture script reads it as written.
   - `frontend_paths`: the directories holding a `package.json` whose dependencies include react, vue, svelte, angular, solid-js, preact, next, nuxt or astro. Separate several with `; `.
   - `stack`: the framework and the component library.
   - `theme`: one of `prefers-color-scheme`, `attribute <name>=<value>`, `class <name>` or `none`.
   - `base_url`, `start` and `start_dir`: take them from the `dev` or `start` script, a Makefile target, or a README line.
   - `fixture`: a demo or fixture command, else `none`.
   - `auth`: `none`, or `required` when a login route guards the root.
   - `login_route` and `login_fields`: when `auth` is `required`, the route of the login form, and the selectors of its user field, its password field and its submit control, separated by `; `. Read the login page and the login helper of the browser tests. Else `none`.
   - `tests`: the browser test runner and its directory, else `none`.
   - `e2e_run`: the command that runs one browser test file, without the file, such as `npx cypress run --spec`. Else `none`.
   - `e2e_dir`: the directory of those test files.
   - `e2e_shots`: the directory where the runner writes screenshots, else `none`.
   - `e2e_start` and `e2e_base_url`: the start command and the base URL the browser tests need, when they differ from `start` and `base_url`. Else `none`.
   - `wait_for`: a root selector when it is obvious, else `none`.
   - `ignore_console` and `ignore_requests`: known dev noise for the framework found, such as Vite's HMR socket.
   - `derived_from`: the repo-relative paths of the files you read, separated by `; `. Write paths only. Leave out a path that has a character other than letters, digits, `.`, `_`, `/` and `-`.
3. Look for a `## UI conventions` heading in the always-on files. Report the file path. Never copy the text.
4. Find the routes the working set's components render. Read route tables and imports.
   - Report each route as a backticked path relative to the base URL, such as `/projects/42`.
   - A path must match `^/[A-Za-z0-9._~/%-]*$`.
   - Give a route with a parameter a concrete example value. Never write a full URL or a screen name.
5. Propose the states to specify for each route: empty, loading, error and phone width. Add dark when `theme` is not `none`. Add no permission when `auth` is `required`. Add the states this task itself needs.
6. Trace the journey when one of these holds. Otherwise skip this step.
   - The task adds a route.
   - The task adds or changes an action that moves the user to another route.
   - The task adds a flow of two or more steps inside one route.

   Use at most six tool calls.
   - List each route the task adds under `### Routes`, marked `(new)`.
   - Find the control that leads to the first route: a link, a menu item or a redirect. Report its `path:line`.
   - Name each control as `<role> "<visible text>"`. Name the file that holds it, or the file that will hold it.
   - Follow each action to where the user lands. Read the handler of the action.
   - Find the guard of each route: the role or the login it needs.
   - Write `unknown` only after a grep for the route and the control's text finds nothing. Never guess a control or a landing.

7. When `e2e_run` is not `none`, plan the browser test of this task. Use at most seven reads.
   - Name the new test file, under `e2e_dir`, in the naming of its neighbours.
   - Find the login helper and the data helpers the existing tests use. Report each with its `path:line`.
   - Name the data this task needs that no helper creates.
   - Find a command the repo runs after a test file is added, if there is one.
   - Find the file the visible text comes from, such as a translation file.
   - Name one existing browser test file that uses the login helper, if there is one.

Report in this order:

- `## UI profile` with the key lines from step 2. Leave out `fingerprint`, `generated` and `capture`, because the planner owns them.
- `### Routes`
- `### Proposed states`
- `### Journey`, only when step 6 ran, with these lines:
  - `actor:` signed in as a role, or signed out.
  - `entry:` the route, the control, the control's file, then `exists <path:line>`, `new` or `unknown`.
  - `hop:` one line per hop: the from-route, the action and its control, the control's file and the to-route. End it with `exists <path:line>`, `new` or `unknown`.
  - `outcome:`, `leaves early:` and `waits:`, each with its text or `unknown`.
- `### E2E`, with these lines:
  - `spec:` the new test file.
  - `login:` the login helper, with its `path:line`.
  - `seed:` the data helpers, each with its `path:line`.
  - `needs:` the data that no helper creates.
  - `text:` the file the visible text comes from.
  - `after_add:` the command the repo runs after a test file is added, else `none`.
  - `smoke:` the existing test of the last bullet of step 7, else `none`.
  - Without a runner, write the single line `none: <reason>`.
- `### Conventions`
- `## Limitations`

If this is not a UI task, report `## UI profile` with the single line `not a UI task`. Add nothing else.
