# WhyGraph v1.x — research: graph backend + agent architecture

## Context

The scaffold from the previous plan is merged. Before implementing features, the user wants to revisit two foundational choices:

1. **Graph backend** — keep `colbymchenry/codegraph` (used by the v0 POC) or switch to `safishamsi/graphify`?
2. **Plugin shape** — should WhyGraph go beyond a passive MCP data-source and orchestrate planning + worker agents that consume rationale cards?

This file is a research note, not an implementation plan. Open questions for the user are at the bottom.

---

## 1. Graph backend comparison

> Numbers below are pulled directly from each repo's source (not summary descriptions).

| Dimension | codegraph (colbymchenry) | graphify (safishamsi) |
|---|---|---|
| Extraction | tree-sitter (WASM) | tree-sitter + optional Claude semantic pass |
| Storage | **SQLite** (tables: `nodes`, `edges`, `files`, `nodes_fts`, `unresolved_refs`) | **NetworkX in-memory → `graph.json`**; optional Neo4j / GraphML / Obsidian export |
| Node kinds | 21 (file, module, class, struct, interface, function, method, property, field, variable, constant, parameter, enum, route, component, …) | ~5 (class, function, method, module, file) |
| Edge types | 11+ (`calls`, `references`, `contains`, `imports`, `exports`, `extends`, `implements`, `type_of`, `returns`, `instantiates`, `overrides`, `decorates`) | 5 (`contains`, `calls`, `imports`, `uses`, `inherits`) — confidence-tagged (`EXTRACTED` / `INFERRED` / `AMBIGUOUS`) |
| Symbol identity | `node_id` keyed by `qualified_name` + `file_path` + line range; persisted across runs in SQLite | SHA256 content-addressed per file (unchanged files reuse cached chunks) |
| Query API | TS library: `findUsages`, `getCallers`, `getCallees`, `getCallGraph`, `getContext`, `buildContext`, `findPath`, `getTypeHierarchy` (+ MCP wrapper) | MCP tools only: `query_graph`, `get_node`, `get_neighbors`, `god_nodes`, `community`, `shortest_path` |
| Packaging | `npm i -g @colbymchenry/codegraph` | `pip install graphifyy` |
| Incremental builds | Re-parses fully each run | SHA256 cache → only changed files re-parsed; has `--watch` mode |
| Stars / contributors / open issues / last push | 654 / 5 / 66 / 2026-04-14 | **41,072 / 16 / 209 / 2026-05-02** |
| Default branch | `main` | `v6` (versioned) |
| WhyGraph v0 already wired up? | **Yes** | No |

### Languages — side-by-side (verified from source)

Codegraph extractor modules live in [`src/extraction/languages/`](https://github.com/colbymchenry/codegraph/tree/main/src/extraction/languages) plus three top-level extractors (DFM, Liquid, Svelte).
Graphify parsers are listed in [`pyproject.toml`](https://github.com/safishamsi/graphify/blob/v6/pyproject.toml) under tree-sitter dependencies.

| Language | codegraph | graphify |
|---|:-:|:-:|
| C | ✅ | ✅ |
| C# | ✅ | ✅ |
| C++ | ✅ | ✅ |
| Dart | ✅ | — |
| DFM (Delphi forms) | ✅ | — |
| Elixir | — | ✅ |
| Go | ✅ | ✅ |
| Java | ✅ | ✅ |
| JavaScript | ✅ | ✅ |
| Julia | — | ✅ |
| Kotlin | ✅ | ✅ |
| Liquid | ✅ | — |
| Lua | — | ✅ |
| Objective-C | — | ✅ |
| Pascal | ✅ | — |
| PHP | ✅ | ✅ |
| PowerShell | — | ✅ |
| Python | ✅ | ✅ |
| Ruby | ✅ | ✅ |
| Rust | ✅ | ✅ |
| Scala | — | ✅ |
| SQL | — | ✅ (extra) |
| Svelte | ✅ | — |
| Swift | ✅ | ✅ |
| TypeScript | ✅ | ✅ |
| Verilog | — | ✅ |
| Zig | — | ✅ |
| **Total** | **18** | **21 (+SQL)** |

**Shared (13):** C, C++, C#, Go, Java, JavaScript, Kotlin, PHP, Python, Ruby, Rust, Swift, TypeScript.
**Codegraph-only (5):** Dart, DFM, Liquid, Pascal, Svelte (front-end templating + Embarcadero/Delphi niche).
**Graphify-only (9):** Elixir, Julia, Lua, Objective-C, PowerShell, Scala, SQL, Verilog, Zig (modern systems + scripting + EDA).

### Recommendation: **still codegraph** — but the case is narrower than I framed before

I had to walk back the "more languages" point — graphify actually supports *more* and *more modern* languages. So the case for codegraph reduces to two real arguments:

1. **SQLite vs JSON is the load-bearing difference.** WhyGraph joins our evidence/rationale tables to the graph's nodes by `node_id`. SQLite gives us indexed joins and FTS. Graphify's NetworkX-in-memory + JSON dump means we'd either shell out to its MCP server for every lookup (no joins) or re-materialize JSON into SQLite ourselves (fighting the tool). The v0 POC's `WHERE node_id = ?` joins have no clean equivalent in graphify.
2. **Richer edge model.** "Why does this exist?" leans on `references`, `overrides`, `decorates`, `instantiates` — codegraph captures all of these as distinct edge types. Graphify collapses non-call refs under a single `uses` edge.

Counter-arguments worth owning honestly:
- Graphify is **far more popular and actively developed** (60× the stars, 3× the contributors, 18-day-newer commit). That signal isn't nothing.
- Graphify's **SHA256 incremental cache + watch mode** are real ergonomics wins; we'd have to roll those ourselves on top of codegraph.
- Graphify is **Python-native** — no subprocess hop or Node-version pain when calling it from a Python WhyGraph.
- Graphify covers languages we may care about later (Scala, Elixir, Lua, PowerShell).

### Where graphify wins (worth borrowing ideas from)

- **SHA256 incremental caching** — codegraph re-parses fully; we can wrap codegraph runs with our own change detection.
- **Confidence-tagged edges** — useful concept; can emulate by storing per-edge confidence in our own evidence table.
- **Watch mode** — ergonomic; could ship as a separate `whygraph watch` command.
- **Community detection / "god nodes"** — interesting for "this symbol is structurally important" hints in rationale, but tangential to v1.

### When to revisit graphify

- If the v0 → v1 port reveals codegraph's TS-only library is annoying to call from Python (likely true — we'd have to either subprocess `codegraph` CLI or reimplement the SQLite query layer in Python). If that pain becomes major, graphify's Python-native API becomes more attractive.
- If multi-modal evidence (linking code to PDFs/diagrams via Claude vision) becomes a real goal.

> **Pragmatic note on the popularity gap:** 41K vs 654 stars is striking, but star counts ≠ fit-for-purpose. Graphify is a generalist code-graph + visualization tool aimed at "understand any codebase." Codegraph is more narrowly designed as an MCP data source for AI agents. WhyGraph's needs match codegraph's narrower target better.

---

## 2. Plugin shape: passive data source vs agent orchestrator

The user is gesturing toward a model where WhyGraph doesn't just answer "give me the rationale for X" but actively orchestrates planning and worker agents that consume rationale cards. Three architectural patterns, increasing in ambition:

### A. Pull-based (today's design)

WhyGraph = MCP server with `whygraph_rationale_pre_edit_brief` and `whygraph_evidence_for`. A skill tells Claude Code to call them automatically before edits. Claude's main agent decides when to ask.

- **Pros:** Simple. Composes with everything — any Claude Code workflow can pull rationale on demand.
- **Cons:** Reactive only. Claude has to know to ask. No batched or proactive use.

### B. Push-based via slash commands (the user's "spawn agents" idea, lightweight version)

Add a `/whygraph-plan <task>` slash command that:

1. Identifies the symbols a task is likely to touch (grep + codegraph queries).
2. Pulls rationale cards for each affected symbol + their callers (1-hop neighbors).
3. Spawns a **Plan subagent** via Claude Code's `Agent` tool, passing the cards as context. The plan agent produces a structured implementation plan.
4. Optionally spawns **N Worker subagents**, each scoped to one slice of the plan with the rationale cards for symbols it'll touch.

WhyGraph still ships its MCP tools (so any agent can pull more cards mid-flight) but now also provides the orchestration that *uses* them well.

- **Pros:** Standard Claude Code primitive (`Agent` tool spawned from a slash command). No external process. Cards flow naturally into both planner and workers.
- **Cons:** Orchestration logic lives in markdown (the slash command body), which is awkward for non-trivial flow control.

### C. Background agent daemon via Claude Agent SDK

A long-running Python process built on the Claude Agent SDK that:

- Watches the repo for changes (or runs on git push).
- Generates rationale cards for changed symbols ahead of time.
- Writes results back into the WhyGraph SQLite cache.
- The MCP server then serves cards instantly with no LLM call on the hot path.

- **Pros:** Cold-start latency on `whygraph_rationale_pre_edit_brief` drops to ~0. Rationale stays fresh automatically. Decouples generation from request-time.
- **Cons:** Significant moving piece. Requires a way to start/stop the daemon, manage credentials, deal with crashes. Probably premature for v1.x.

### Cross-cutting: what is a "rationale card"?

The user described it as: `node + connected nodes (usages) + git evidence + github evidence → rationale`. Concretely a card might look like:

```
## auth.middleware.session_validator (function)
src/auth/middleware.py:42–88

**Why:** Replaces legacy cookie validator after compliance audit (2025-Q4).
**Constraints:** must be sync (called from request hot path); rejects when token TTL < 30s.
**Tradeoffs:** runs on every request — cached the JWK set lookup to avoid latency.
**Risks:** changes to claim shape break downstream `RoleResolver`.

**Used by (callers):**
- `web.api.middleware_chain` (src/web/api.py:120)
- `worker.queue.message_handler` (src/worker/queue.py:55)

**Evidence:**
- 7 commits, 3 authors, last touched 2026-03-12
- PR #847 ("Compliance fix for session token storage")
- Issue #802 (legal review trigger)
```

Two design questions follow from this:
- **Where does the planner get cards?** Inlined into the slash command body at spawn time (one big context dump), or the planner agent calls `whygraph_rationale_pre_edit_brief` per symbol it cares about? Inline = simpler but token-heavy; on-demand = cheaper but planner has to know which symbols matter first.
- **Where do worker agents get cards?** Same question, recursively.

### Recommendation: **start with B (push via slash command), keep MCP tools as the primitive**

- Pattern B layers cleanly on top of pattern A — the MCP tools stay, we just add orchestration.
- Cards are the unit of context shared between planner and workers — design them once, reuse everywhere.
- C is interesting but premature: until card generation is observably slow, the daemon adds ops complexity without payoff.

---

## Decisions captured this round

**Architecture:**

- **`GraphBackend` abstraction inside WhyGraph.** Python protocol with methods like `get_node(qname)`, `get_callers(node_id)`, `get_callees(node_id)`, `find_symbols(query)`. WhyGraph's rationale/evidence layer talks to *the interface*, never to the backend's storage directly.
- **First implementation: `SqliteCodegraphBackend`** — reads codegraph's SQLite directly (no subprocess, no MCP roundtrip). Reuses the v0 reader logic.
- **Future implementations available without re-architecting:** `JsonGraphifyBackend` (loads `graph.json`), `MCPBackend(server_command)` (true MCP-to-MCP composition if we ever want it).
- **WhyGraph's own MCP surface stays narrow** — rationale and evidence cards only. Users who want raw graph queries install the graph backend's own MCP server alongside.

**Plugin shape (in order):**

1. **MCP tools first** — `whygraph_rationale_pre_edit_brief`, `whygraph_evidence_for`. These are the primitives every workflow uses.
2. **Then a `/whygraph-plan <task>` slash command** that spawns a Plan subagent via Claude Code's `Agent` tool, with rationale cards **inlined at spawn time** (slash command computes affected symbols, dumps cards into the planner's prompt).
3. Workers come after the planner is working.

**Why not the alternatives we considered:**

- *Skip abstraction, commit to codegraph directly* — locks us to one backend; refactor cost is high if graphify's ecosystem keeps pulling ahead.
- *Real MCP-to-MCP composition (WhyGraph as MCP client of graph MCP)* — adds a second graph-MCP process per project, per-call stdio latency, manual lifecycle management. Premature.
- *Background daemon (Claude Agent SDK pre-generates rationale)* — significant ops burden, payoff only if card generation latency becomes observably bad. Defer.

## Still open (for the next planning round, when we move to implementation)

1. **Card schema details** — is the sketch above (purpose / constraints / tradeoffs / risks / callers / evidence) the final shape? Do cards include "similar symbols" via community detection, or stay strictly local to the symbol + 1-hop neighbors?
2. **Worker shape** — single Worker subagent that executes the whole plan, or N workers each scoped to one slice of the plan? Affects how cards are partitioned at spawn time.
3. **Trigger surface beyond `/whygraph-plan`** — also auto-fire from a Claude Code hook (e.g. "before any Edit tool, generate cards for the file's symbols"), or keep manual?
4. **`GraphBackend` interface granularity** — what's the minimum set of methods we need to support both codegraph (rich) and graphify (simpler) cleanly? Probably: `get_node`, `get_callers`, `get_callees`, `find_symbols`, `walk_neighbors(depth)`. To be nailed down when designing the interface.
5. **Cache key under backend swap** — if we ever switch from codegraph to graphify, our `(node_id, prompt_version, model)` cache key invalidates because the ID space changes. Need a content-addressable layer (e.g. hash of `qualified_name + file_path`) in the abstraction so cards survive a backend change.
