# The Chat assistant

The second view in [`whygraph serve`](playground.md) is a chat assistant that answers questions about
your repo by calling WhyGraph's own tools. Ask *"why does the rationale cache key on qualified name
instead of node id?"* or *"who has touched the scan crawler in the last year?"* and it goes and looks
- reading rationale cards, walking evidence, querying the graph, running aggregate SQL, and drawing
charts from the results.

Every tool it calls is the **same in-process function** the MCP server or the Explorer already calls.
There is no second implementation and no MCP roundtrip, so the assistant cannot drift from what your
editor sees.

!!! warning "It costs money and it writes to your database"
    Unlike the Explorer, the assistant is not read-only. Every turn calls an LLM under your `[chat]`
    provider, and every message, tool call, and session is stored in the WhyGraph database. It is
    still loopback-only with no auth (see [Localhost only](playground.md#lifecycle)).

## Getting started

Run the server and switch to **Chat** in the header:

```bash
whygraph serve
```

Pick a provider and model in the composer, type a question, and send. The first thing to know is that
answers are grounded: when the assistant makes a claim it shows the tool calls behind it, and you can
expand any of them to see what came back.

Symbols in an answer are links. The assistant writes them as `whygraph://symbol/<qualified_name>`,
and clicking one jumps to that symbol in the Explorer - graph recentered, detail panel open.

## Providers

Chat needs **streaming tool calls**, which not every adapter supports. Four of WhyGraph's six LLM
providers can drive it:

| Provider | Chat | Env var |
|---|---|---|
| `anthropic` | Yes | `ANTHROPIC_API_KEY` |
| `openai` | Yes | `OPENAI_API_KEY` |
| `deepseek` | Yes | `DEEPSEEK_API_KEY` |
| `openrouter` | Yes | `OPENROUTER_API_KEY` |
| `ollama` | No | - |
| `claude-cli` | No | - |

`ollama` is excluded because local models' tool-calling reliability varies too much to depend on;
`claude-cli` disables tools outright. Both still work for `[analyze]` and `[rationale]`. See
[LLM providers](../reference/llm-providers.md).

Providers you haven't configured still appear in the picker, greyed out, labelled with the env var
they need - so a missing key looks like a missing key rather than a missing feature.

!!! note "The model list may fall back"
    The picker asks your provider for its live model list. If your API key is scoped narrowly it can
    work for chat but return `401` on the models endpoint - in that case WhyGraph shows a short
    built-in list instead. That is normal operation, not an error.

### Choosing a default

`whygraph init` doesn't ask about chat - you pick a provider and model per session in the composer.
`[chat] provider` and `model` in `whygraph.toml` set the **defaults for new sessions**, and nothing
more: each session records the pair it was started with, so changing your config never rewrites an
existing conversation's history or re-answers it with a different model.

```toml
[chat]
provider = "anthropic"
# model = "claude-opus-4-7"   # default: the provider's own [llm.*] model
```

If OpenRouter is your provider, pin a tool-capable model rather than leaving `openrouter/auto` -
automatic routing can land on a model that does not support tool calling.

## Sessions

The left pane is your session list. Sessions are stored in the WhyGraph database alongside the
evidence, so they survive restarts and are per-repo, not global.

- A new session is **auto-titled** from your first message.
- Sessions are deep-linkable at `/chat/<id>` - copy the URL to come back to a conversation.
- Rename and delete are in the session list.

## What it can look at

Seventeen tools across five sources. You never call these directly; they are listed so you know what
the assistant can and cannot reach.

<div class="grid cards" markdown>

-   __Code structure__ (CodeGraph)

    ---

    `search_symbols`, `get_symbol`, `get_area_outline`

-   __Evidence and rationale__ (WhyGraph)

    ---

    `get_rationale`, `get_evidence`, `get_area_history`, `find_changes`, `get_commit`, `get_pr`,
    `get_issue`, `get_repo_overview`, `list_recent_activity`

-   __Statistics__ (aggregate SQL)

    ---

    `run_project_stats` over commit history, `run_graph_stats` over the CodeGraph index

-   __Charts__

    ---

    `render_chart`

-   __Source tree__

    ---

    `read_file`, `list_dir`

</div>

### Statistics are aggregate-only

The two `*_stats` tools let the assistant write its own SQL, which is why they are fenced hard. Every
query runs through a SQLite authorizer that permits **read-only aggregates** and nothing else, over an
explicit table allowlist. Results cap at **200 rows** with a **5-second** deadline.

The allowlist deliberately excludes `chat_*` (the assistant's own transcripts) and `rationale_cache` -
the assistant cannot query its own conversations, and coverage is reported through the proper tools
rather than by reading the cache table.

### Reading files

`read_file` returns at most **400 lines or 100 KB** per call, `list_dir` at most **200 entries**, and
both refuse anything under `.git/`, `.whygraph/`, or `.codegraph/`, plus `whygraph.toml` itself - your
live config holds real API keys. WhyGraph's own storage is reachable only through the knowledge tools,
which present it in a curated shape instead of as raw database rows.

## Charts

Ask for a chart and the assistant draws one from a statistics result - *"chart commits per month for
the last two years"*, *"break down changes by author as a stacked bar"*.

Five kinds are available: `line`, `bar`, `bar_h`, `bar_stacked`, and `bar_h_stacked`. Stacked charts
carry at most six series.

Charts are drawn from data the assistant has **already fetched**, never from numbers it wrote itself.
A statistics tool mints an opaque reference for its result set, and `render_chart` can only point at
one of those references - so a chart is always a view onto real rows. References expire at the end of
the turn, and none is minted at all for an errored, truncated, or trivially short (fewer than two
rows) result, which is why the assistant will sometimes tell you there is nothing worth plotting.

Every chart ships with a **Table** toggle showing the underlying rows, and a PNG export.

## Bounds on a turn

Tuned under `[chat]` in `whygraph.toml`:

| Key | Default | What it bounds |
|---|---|---|
| `max_tool_rounds` | `8` | Tool rounds per user turn. On the last round the assistant is called once more without tools, so a turn always ends in prose rather than mid-investigation. |
| `max_rationale_generations` | `2` | Uncached rationale cards one turn may generate. `0` makes the assistant cache-only. |
| `context_token_budget` | `60000` | Conversation history sent to the model. |

Two behaviours worth knowing:

- **A failing tool never ends a turn.** Errors come back to the model as results, so it can correct
  course and try something else. Individual results are truncated at 30 000 characters with an
  explicit marker.
- **Rationale generated inside chat uses your `[rationale]` provider**, not the chat session's. That
  keeps the cache key identical to what the Explorer and the MCP tools produce, so a card generated
  from a conversation is the same card your editor gets.

When history outgrows `context_token_budget`, WhyGraph first elides stale tool results, then drops
whole turns from the top - never splitting a tool call from its result.

## Limits

- One repo per server, the one you ran `whygraph serve` in.
- No auth and no multi-user support; it is a local dev tool.
- The assistant cannot edit your code. It reads, queries, and charts.
