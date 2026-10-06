# The Explorer

The [portal](../portal/index.md) opens a web view onto everything WhyGraph and CodeGraph have built for
each project. Open a project and you get two views, switched from the sidebar:

- **Explorer** - browse the code graph, jump to any symbol, and read its rationale, evidence,
  relationships, and history side by side. This page.
- **Chat** - ask questions in English and let an assistant call WhyGraph's tools to answer them. See
  [The Chat assistant](chat.md).

Both are backed by the same data the MCP tools serve - the web API is just a second transport over the
exact same functions, so the panel can never drift from what your editor sees.

## Open it

Start the portal, then open a project from the Projects page:

```bash
whygraph up
```

The portal is at <http://127.0.0.1:8765> by default. The Explorer for a project is at `/p/<slug>/explorer`.
See [Start the portal](../portal/start.md) for the port, the other host commands and stopping it.

!!! note "Scan first"
    The panel reads the CodeGraph index and the WhyGraph evidence database. A project's first scan
    (the last step of [adding it](../portal/projects.md#first-scan)) fills them - until then there's no
    graph to draw, and every symbol's rationale shows *"no evidence"* (see
    [Rationale on demand](#rationale-on-demand)).

!!! info "Localhost only"
    The portal is published to `127.0.0.1` only and has **no login in local mode** - it is a single-user tool for
    your own machine. Nothing is exposed beyond its loopback. See the
    [security model](../portal/security.md).

    The Explorer is read-only apart from the explicit **Generate rationale** button. The
    [Chat assistant](chat.md) is not: it calls an LLM and stores sessions and messages in the
    project's WhyGraph database.

## What you see in the Explorer

<div class="grid cards" markdown>

-   __Left - containment tree__

    ---

    `directory → file → class → method`, lazy-loaded. Click a symbol to open it.

-   __Center - graph__

    ---

    The **overview** (directory super-nodes, colored by rationale coverage) is the landing view;
    click a directory to expand it. Pick a symbol and the center switches to its **ego graph** -
    what it calls, is called by, imports, and contains.

-   __Right - detail panel__

    ---

    Tabs for **Relationships**, **Rationale**, **Evidence**, and **History** on the selected symbol.

-   __⌘K - search__

    ---

    Find any symbol by name (disambiguated by file path), `Enter` to open it - recentering the
    graph, opening the panel, and revealing it in the tree.

</div>

Every symbol reference in the panel - a search hit, a graph node, a relationship row - opens the same
way, so you can navigate the codebase by following edges. A `whygraph://symbol/...` link in a
[chat](chat.md) answer opens a symbol here too.

### How the overview stays readable

A real repo has far too many `calls` and `imports` edges to draw at once, so the overview doesn't
draw them. It **lifts** each edge onto the deepest currently-visible ancestor of both endpoints:
cross-directory edges become one weighted arrow between super-nodes, and edges wholly inside a
collapsed directory are hidden and counted instead. Expand a directory and the edges beneath it
resolve into finer detail.

### Rationale on demand

Generating a rationale card calls an LLM, so the panel never does it behind your back. The
**Rationale** tab shows a cached card if one exists; otherwise it shows a **Generate rationale**
button. Click it, watch the loading state, and the card renders - and is cached, exactly as if the
MCP tool had produced it.

The button is **disabled** when the symbol has no historical evidence to reason from - most commonly
because the repo hasn't been scanned, or the code isn't committed yet. Run `whygraph scan` and the
button lights up. The **Evidence** and **History** tabs never call an LLM, so they always work.

Generation uses the rationale model configured for the project - the **rationale** override or the
default model, with the provider key from Settings - the same provider and model the MCP tool uses.
If that provider has no key yet, add one under Settings first. See
[Configuration](../reference/configuration.md).

### Coverage heatmap

Because rationale cards are generated lazily, the overview colors each directory and file by how much
of it has been analyzed - a quick map of where you've already asked "why?" and where you haven't.

## Develop the UI

The panel's source lives at `src/playground/` (Vite + React + TypeScript). `make dev-local` runs a
dev portal with the Vite dev server in front of it, so edits hot-reload at `http://localhost:5174`;
`make playground` builds the production bundle into the wheel's static directory. See
[Developing WhyGraph](developing.md) for every development mode.

## Not in scope

The panel is a local tool, and stays one: **no login and no multi-user support in local mode**, **no remote
hosting**, and **no editing** - it never writes to your source tree. See the
[roadmap](../roadmap.md) for what's deferred.
