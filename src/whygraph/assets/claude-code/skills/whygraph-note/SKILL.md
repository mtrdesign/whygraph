---
name: whygraph-note
description: Propose a WhyGraph project note - a decision, constraint, gotcha or convention anchored to code - for a person to confirm in the portal.
argument-hint: "<what to remember>"
allowed-tools: mcp__whygraph__whygraph_propose_note
disable-model-invocation: true
---

Arguments: `$ARGUMENTS`

The arguments are the text of the note. If empty, respond with: `Usage: /whygraph-note <what to remember>` and stop.

1. Pick the `kind`: `decision`, `constraint`, `gotcha` or `convention`.
2. Find the anchor. Use what the conversation already makes clear: a `qualified_name`, or a `path` (with `line_start` and `line_end` for a range), or an `area`, or `project_wide: true` for a rule about the whole project. If it is not obvious, ask the user which one, and wait for the answer.
3. Call the MCP tool `whygraph_propose_note` with `body` (the text, tightened to one paragraph), `kind` and that anchor. Give exactly one anchor mode.
4. Print the note id and the review link the tool returns (a portal page path). A person must confirm the note there before it counts; say so in one line.

If the tool returns `isError: true`, print the error message verbatim and stop. Do not edit files or run other tools.
