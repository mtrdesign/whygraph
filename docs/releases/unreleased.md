# Unreleased

!!! note "Not released yet"
    These changes are on `main` and will ship in the next release. Nothing here is in a published
    image yet.

## What's new

- **Shared plans.** A plan your team commits is now part of what WhyGraph knows. The scan has a new
  **Plans** phase that indexes the Markdown files under `[plans].dir` (default `docs/plans`) from
  `HEAD`'s git objects, with no model call, and links each plan to code through `WhyGraph-Plan:` lines
  in commit messages and PR descriptions and through the paths the plan names. A plan's status
  (`shipped`, `in_flight` or `not_implemented`) is derived from those links, and a commit or PR that
  only adds the plan file does not count. See [Sharing plans with your team](../guide/planning.md#sharing-plans).
- **Publish a plan.** A passing `/whygraph-implement` run copies its plan into the plans folder on its
  own and prints the `git add` line (opt out with `--no-publish`). `/whygraph-plan --publish <stem>
  [--force]` does it on demand. A re-publish replaces WhyGraph's own earlier copy but never one you
  edited by hand unless you pass `--force`. The implementor now writes `WhyGraph-Plan: <stem>` on the
  commits it makes. Run **Update agent files** in each project to get the new commands.
- **Plans for agents, as intent.** `whygraph_rationale_brief` and `whygraph_area_history` may carry a
  short `plans` list of pointers, and two new resources, `whygraph://plan/{stem}` (a decision digest
  and contents) and `whygraph://plan/{stem}/section/{n}` (one section of at most 8,000 characters),
  are the only way plan text reaches an agent. Plans are intent, ranked below commits and PRs;
  `whygraph_evidence_for` carries none. Linked checkouts get them from the platform, which serves
  three new `/api/v1` routes. See [the MCP reference](../reference/mcp.md#plans).
- **A Plans page.** A read-only page per project lists the committed plans with status tabs and search
  and opens a plan with its commits, PRs and paths. The Explorer's Rationale tab links the plans
  behind a symbol, and **Settings > Plans** turns indexing off or changes the folder.
- **`plans_dir` in `portal.env`.** The untracked `.whygraph/portal.env` gains a third line so every
  editor, VS Code included, publishes into the right folder.
