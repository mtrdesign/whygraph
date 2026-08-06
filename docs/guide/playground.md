# The Explorer

`whygraph serve` opens a local web panel onto everything WhyGraph and CodeGraph have built for the
current repo. It has two views, switched from the header:

- **Explorer** - browse the code graph, jump to any symbol, and read its rationale, evidence,
  relationships, and history side by side. This page.
- **Chat** - ask questions in English and let an assistant call WhyGraph's tools to answer them. See
  [The Chat assistant](chat.md).

Both are backed by the same data the MCP tools serve - the web API is just a second transport over the
exact same functions, so the panel can never drift from what your editor sees.

It runs from the **same Docker image** as every other command, as its own long-lived container - no
second image, no extra install.

## Run it

From a scanned repo:

```bash
whygraph serve
```

That starts the server in the foreground and prints a URL - open <http://localhost:8765>. `Ctrl-C`
stops it.

!!! note "Scan first"
    The panel reads the CodeGraph index and the WhyGraph evidence database. Run
    [`whygraph scan`](scanning.md) at least once before serving - otherwise there's no graph to draw,
    and every symbol's rationale shows *"no evidence"* (see [Rationale on demand](#rationale-on-demand)).

### Lifecycle

On the Docker install the shim manages the container for you:

| Command | What it does |
|---|---|
| `whygraph serve` | Run in the foreground; `Ctrl-C` stops and removes the container. |
| `whygraph serve --detach` (or `-d`) | Start in the background and return immediately. |
| `whygraph serve --logs` | Tail the detached server's logs. |
| `whygraph serve --stop` | Stop and remove the running server. |
| `whygraph serve --help` | Show the in-container `serve` help. |

The port is controlled by the `WHYGRAPH_PORT` environment variable (default `8765`):

```bash
WHYGRAPH_PORT=9000 whygraph serve --detach
```

!!! warning "The port is set by the environment, not a flag"
    On the Docker install the shim owns the published port, so `whygraph serve --port 9000` is
    **rejected with an error** rather than quietly ignored - use `WHYGRAPH_PORT`. Running WhyGraph
    natively (`uv` / `pipx`), `--port` and `--host` work as normal flags.

!!! info "Localhost only"
    The server is published to `127.0.0.1` only - it's a single-user local dev tool with **no auth**.
    Nothing is exposed beyond your machine's loopback.

    The Explorer is read-only apart from the explicit **Generate rationale** button. The
    [Chat assistant](chat.md) is not: it calls an LLM and stores sessions and messages in the
    WhyGraph database.

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

Generation uses the rationale LLM you configured in `whygraph.toml` - `[rationale] provider` and the
matching `[llm.<provider>]` (with its `api_key`), exactly as `whygraph init` sets it up and the same
provider the MCP tool uses. If you leave `api_key` unset, the provider's conventional env var (e.g.
`ANTHROPIC_API_KEY`) is the fallback; the Docker container reads your repo's `whygraph.toml` directly.
See [Configuration](../reference/configuration.md).

### Coverage heatmap

Because rationale cards are generated lazily, the overview colors each directory and file by how much
of it has been analyzed - a quick map of where you've already asked "why?" and where you haven't.

## Develop the UI

The panel's source lives at `src/playground/` (Vite + React + TypeScript). For a hot-reloading dev
loop - the backend on `:8765` and the Vite dev server on `:5173`, proxying the API across:

```bash
make dev      # backend + Vite HMR together; Ctrl-C stops both; open :5173
```

Other targets: `make playground` builds the production bundle into the wheel's static directory, and
`make serve` builds it then serves it the way it ships. All need Node ≥ 18 (`nvm use 22`).

## Not in scope

The panel is a local dev tool, and stays one: **no auth and no multi-user support**, **no remote
hosting**, **one repo per server** (the one you ran it in), and **no editing** - it never writes to
your source tree. See the [roadmap](../roadmap.md) for what's deferred.
