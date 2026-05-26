# /rationale — show the WhyGraph rationale for a code chunk

Show the WhyGraph rationale for a code chunk — purpose, why, constraints, tradeoffs, risks. Read-only lookup.

Parse the user's input as `<target> [--force]`:

- First non-flag token → `target`. Two forms are accepted:
  - A qualified_name like `whygraph_rationale_brief` or `auth.session.refresh_token` → pass as `qualified_name`.
  - A `path:line_start-line_end` triple like `src/whygraph/mcp_server.py:957-1056` → split into `path`, `line_start`, `line_end`.
- `--force` → pass `force_refresh: true` (bypasses the rationale cache).

If no target was provided, respond with: `Usage: /rationale <qualified_name | path:line_start-line_end> [--force]` and stop.

Otherwise call the MCP tool `whygraph_rationale_brief` with those arguments, then present the result verbatim. The tool returns a structured object with `purpose`, `why`, `constraints[]`, `tradeoffs[]`, `risks[]`, `confidence`, `evidence_count`, and `cached`. Render it as readable markdown — do not paraphrase or editorialise the rationale fields themselves.

If the tool returns `isError: true`, print the error message verbatim and stop.

Do not edit files or run other tools. This is a read-only lookup.
