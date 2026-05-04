---
description: Show the WhyGraph rationale for a code symbol — purpose, why, constraints, tradeoffs, risks. Read-only lookup.
---

Arguments: `$ARGUMENTS`

Parse them as `<target> [--json] [--refresh] [--force]`:
- First non-flag token → `target` (a CodeGraph node ID like `function:abc123` or a qualified_name like `Page`).
- `--json` → pass `response_format: "json"` (default is `"markdown"`).
- `--refresh` → pass `refresh_evidence: true`.
- `--force` → pass `force: true`.

If no target was provided, respond with: `Usage: /rationale <target> [--json] [--refresh] [--force]` and stop.

Otherwise call the MCP tool `whygraph_rationale_pre_edit_brief` with those arguments, then print the tool's text output **verbatim** — do not paraphrase, summarize, or comment. If the tool returns `isError: true`, print the error message verbatim and stop.

Do not edit files or run other tools. This is a read-only lookup.
