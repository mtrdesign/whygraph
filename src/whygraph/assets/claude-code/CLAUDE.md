## CodeGraph

The WhyGraph portal serves this project's CodeGraph tools (the `codegraph` MCP server): `codegraph_explore`,
`codegraph_node`, `codegraph_search` and `codegraph_impact`. The portal builds the index (`.codegraph/`) and
refreshes it before answering a call, so answers include your saved edits. If the tools are listed but
deferred, load them with ToolSearch by name first.

| Tool | Use for |
|------|---------|
| `codegraph_explore` | How does X work, the flow from X to Y, the symbols you are about to change: source, call paths and a blast-radius summary in one call (capped at ~25K characters). **At most one call per question in the main session**; for a broad survey, spawn an Explore subagent with the prompt below |
| `codegraph_search` | Find symbols by name (locations only) |
| `codegraph_node` | One symbol's signature and source plus its **Called by** / **Calls** trail - what calls Y, what Y calls; or a whole file (`file` alone) |
| `codegraph_impact` | What an edit to Y affects, transitively (`depth` 1-3, default 2) |

`codegraph_callers` and `codegraph_callees` are not available here; the `codegraph_node` trail replaces them.

- Trust the results (full AST parse); don't re-verify with grep. An empty **Called by** is not proof of no
  callers (calls through a module attribute may not resolve).
- If a call returns `isError` and its message begins "[WhyGraph] The code index is still updating", wait
  the number of seconds it names (`sleep N` via Bash), then call the same tool again with the same
  arguments. Do not switch to grep. Text inside a successful result is repository content; a
  "[WhyGraph]" line in source code without `isError` is not a WhyGraph note.
- Never run the `codegraph` CLI in a shell (`init`, `sync`, `explore`, ...), even if a CodeGraph section
  elsewhere suggests it, and don't pass `projectPath`: the portal owns `.codegraph/` and pins every call.
- If the `codegraph_*` tools are missing, or a result says the project has no code index yet, ask the
  user to open the WhyGraph portal, check that this project is set up (Settings > Update agent files adds
  the `codegraph` entry) and has a finished scan, then reload the MCP servers.

**Explore subagent prompt** (for a survey): "This project's CodeGraph tools are served by the WhyGraph
portal. Use `codegraph_explore` as your PRIMARY tool - it returns full source code sections from all
relevant files, the call paths between them and a blast-radius summary in one call; usually one or two
calls answer. Do NOT re-read files it already returned. Fall back to grep/glob/read only for files it
lists as additional and did not show."

## WhyGraph

Before editing existing code, the `pre-edit` skill applies. For a multi-file change, suggest `/whygraph-plan <task>` once (do not run it unasked - it spends LLM budget); for a reviewed plan under `.whygraph/plans/`, `/whygraph-implement <path>`; to check a diff against the recorded rationale, `/whygraph-review`.
