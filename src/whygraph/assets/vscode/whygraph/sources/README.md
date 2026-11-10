# Research sources

Each file in this folder is one source of knowledge for `/whygraph-plan`. The planner reads the frontmatter of every file here, keeps the sources whose tools exist, and starts one researcher per kept source. The researcher reads the file body and follows it.

To add a source, write one file here. Delete a file you added to remove its source; to switch off a bundled one, list it in `.whygraph-overrides/disabled`. To change a bundled source, put a replacement or an `.append.md` file in `.whygraph-overrides/` instead of editing it here. Your own files survive an update of the agent files.

## File format

A source file is frontmatter plus a body. Put no comments in the frontmatter. The parser reads each whole value.

```markdown
---
name: repo-tests
description: Which literal commands prove each step, and which symbols no test exercises.
requires: codegraph_search
tools: read, codegraph_search, codegraph_node
section: Verify candidates
enabled: true
budget: 20
consent: false
---

*Question:* ... (the body: a numbered procedure, what to report under the section, what to skip)
```

| Key | Meaning |
|---|---|
| `name` | The name of the report. Use kebab-case. |
| `description` | One line. The consent question shows it for a source that you added. |
| `requires` | Bare tool names, separated by commas. The planner keeps the source only when every tool is callable. Empty means always eligible. |
| `tools` | The tools this source may use: `read`, `web` and bare MCP tool names. It narrows the researcher's tool list and never widens it. A shell, a file write and a page fetch are never valid. |
| `section` | The heading of the report section this source fills. The synthesizer reads sections, not names. Each source needs a unique section. |
| `enabled` | `false` keeps the file but never starts it. `--sources` turns it on for one run. |
| `budget` | An integer. The researcher makes at most this many tool calls for this source. |
| `consent` | `true` forces the consent question. It cannot switch off a question that the slash command computes. |

## Tool names

Write a tool by its bare name, for example `codegraph_search`, `whygraph_area_history` or `search_docs`. A bare name matches a session tool whose name is equal to it, or ends in `__<name>` or `/<name>`. The word `read` means reading and searching files: `Read`, `Grep` and `Glob` in Claude Code, and the matching tools in the other editors. The word `web` means searching the web with the editor's own search tool: `WebSearch` in Claude Code. It never means opening a page, and a source that names it always asks first.

## When the user is asked

The slash command asks once per run, with the default No, before it starts a kept source that:

- names an MCP tool other than a `whygraph_*` or `codegraph_*` tool, or the word `web`, in `requires` or `tools`;
- is not one of the six bundled files (`code-structure.md`, `whygraph-rationale.md`, `repo-tests.md`, `repo-history.md`, `ui-surface.md`, `best-practices.md`);
- says `consent: true`.

A file you add always triggers the question. A file cannot switch the question off.

## Worked example: a source that reaches an outside service

WhyGraph bundles one such source, `best-practices.md`: it searches the web with the `web` word and always asks first. Its findings rank below this repository's own evidence (see `## Evidence order` in `plan-spine.md`). For another service, suppose you have an MCP server named `docs` with a tool `search_docs`. A source file for it would set:

- `requires: search_docs` keeps it out of any project without that server.
- `consent: true` makes the slash command ask, and show the search terms, before any search runs.
- `tools` lists only that server's tools.
- The body gives the order of calls, the budget, the rule to stop early, and the rule to cite and paraphrase.

The `whygraph-researcher-external` agent ships with web search and no outside MCP server. In Claude Code, grant your server with an append file in `.whygraph-overrides/claude-code/agents/researcher-external.append.md` whose frontmatter is `tools: mcp__<server>__*` (see the agent-file overrides docs). The Cursor and Codex agent files have no tool list.
