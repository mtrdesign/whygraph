---
name: ui-surface
description: The project's UI surface: stack, how to start it, its routes, and the states a UI task should specify.
requires:
tools: read, codegraph_search
section: UI profile
enabled: true
budget: 20
consent: false
---

*Question:* what does this project's UI look like, how do you start it, and which routes and states does this task touch?

1. If the prompt carries a `PROFILE:` block, use it as the profile. Skip to step 4.
2. Derive the profile with at most twelve reads. Report each item as a `key: value` line.
   - `frontend_paths`: the directories holding a `package.json` whose dependencies include react, vue, svelte, angular, solid-js, preact, next, nuxt or astro. Separate several with `; `.
   - `stack`: the framework and the component library.
   - `theme`: one of `prefers-color-scheme`, `attribute <name>=<value>`, `class <name>` or `none`.
   - `base_url`, `start` and `start_dir`: take them from the `dev` or `start` script, a Makefile target, or a README line.
   - `fixture`: a demo or fixture command, else `none`.
   - `auth`: `none`, or `required` when a login route guards the root.
   - `tests`: the browser test runner and its directory, else `none`.
   - `wait_for`: a root selector when it is obvious, else `none`.
   - `ignore_console` and `ignore_requests`: known dev noise for the framework found, such as Vite's HMR socket.
   - `derived_from`: the files you read.
3. Look for a `## UI conventions` heading in the always-on files. Report the file path. Never copy the text.
4. Find the routes the working set's components render. Read route tables and imports.
   - Report each route as a backticked path relative to the base URL, such as `/projects/42`.
   - A path must match `^/[A-Za-z0-9._~/%-]*$`.
   - Give a route with a parameter a concrete example value. Never write a full URL or a screen name.
5. Propose the states to specify for each route: empty, loading, error and phone width. Add dark when `theme` is not `none`. Add the states this task itself needs.

Report in this order:

- `## UI profile` with the key lines from step 2. Leave out `fingerprint`, `generated` and `capture`, because the planner owns them.
- `### Routes`
- `### Proposed states`
- `### Conventions`
- `## Limitations`

If this is not a UI task, report `## UI profile` with the single line `not a UI task`. Add nothing else.
