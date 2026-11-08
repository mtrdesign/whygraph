---
name: code-structure
description: Which working-set symbols sit on an external contract, and who calls them from outside.
requires: codegraph_node
tools: read, codegraph_node, codegraph_search
section: Boundaries
enabled: true
budget: 20
consent: false
---

*Question:* which symbols in this change are external contracts, and which belong to another part of the work?

1. For each working-set file, call `codegraph_node(file=<path>, symbolsOnly=true)` to list its symbols.
2. Mark each symbol that is an external contract: a public API, a persisted schema, an IPC message, an exported symbol or a route. Give the `file:line` that shows why.
3. For each marked symbol, list the callers outside the working set. Use `codegraph_search` when `codegraph_node` does not show them.
4. When the prompt sets `PARENT:`, find each symbol that is in another child's `files_hint`. Mark it `owned by t<N>`, with N the id of that child.
5. Report one bullet per symbol: `- `qualified.name` (path:LN) - external contract | owned by t<N> | internal - <one-line reason>`.

Skip symbols that are `internal` and have no caller outside the working set.
