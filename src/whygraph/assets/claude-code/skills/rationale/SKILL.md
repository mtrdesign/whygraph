---
name: rationale
description: Show the WhyGraph rationale for a code chunk — purpose, why, constraints, tradeoffs, risks. Read-only lookup.
argument-hint: <qualified_name | path:line_start-line_end> [--force]
allowed-tools: mcp__whygraph__whygraph_rationale_brief
disable-model-invocation: true
---

Arguments: `$ARGUMENTS`

Parse them as `<target> [--force]`:
- First non-flag token → `target`. Two forms are accepted:
  - A qualified_name like `whygraph_rationale_brief` or `auth.session.refresh_token` → pass as `qualified_name`.
  - A `path:line_start-line_end` triple like `src/auth/session.py:120-180` → split into `path`, `line_start`, `line_end`.
- `--force` → pass `force_refresh: true` (bypasses the rationale cache).

If no target was provided, respond with: `Usage: /rationale <qualified_name | path:line_start-line_end> [--force]` and stop.

Otherwise call the MCP tool `whygraph_rationale_brief` with those arguments, then present the result verbatim. The tool returns a structured object with `purpose`, `why`, `constraints[]`, `tradeoffs[]`, `risks[]`, `evidence_count`, and `cached_at`. Render it as readable markdown — do not paraphrase or editorialise the rationale fields themselves.

If the tool returns `isError: true`, print the error message verbatim and stop. If it is `code: budget_exceeded` or `generation_not_permitted`, do not retry: say that generation is paused for this project and that `whygraph_evidence_for` still lists the raw commits and PRs.

Do not edit files or run other tools. This is a read-only lookup.
