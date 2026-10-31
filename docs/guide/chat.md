# The Chat assistant

The second view of a project in the [portal](../portal/index.md) is a chat assistant that answers
questions about your repo by calling WhyGraph's own tools. Ask *"why does the rationale cache key on qualified name
instead of node id?"* or *"who has touched the scan crawler in the last year?"* and it goes and looks
- reading rationale cards, walking evidence, querying the graph, running aggregate SQL, and drawing
charts from the results.

Every tool it calls is the **same in-process function** the MCP server or the Explorer already calls.
There is no second implementation and no MCP roundtrip, so the assistant cannot drift from what your
editor sees.

!!! warning "It costs money and it writes to your database"
    Unlike the Explorer, the assistant is not read-only. Every turn calls an LLM under your chat
    provider, and every message, tool call, and session is stored in the project's WhyGraph database.
    Each model round is recorded, with its tokens and an estimated cost, on the portal's
    [Usage & cost](../portal/usage.md) page (counts and cost only, never the chat itself).
    The portal is loopback-only with no login in local mode (see [Localhost only](playground.md#open-it)).

If an organization, project or member [budget](../portal/usage.md#budgets-and-the-hard-stop) has the
hard stop on and is used up, the composer is replaced by a notice and a turn already running ends
with "This chat has reached its monthly budget. You can still read everything that's already
generated." Earlier conversations stay readable. The stop lifts when the budget is raised or the
month (UTC) rolls over.

## Getting started

Start the portal (`whygraph up`), open a project, and press **+** next to **Chats** in the sidebar
(or press `g` then `c`, or pick **New chat** in the command menu). You need a key for at least one of
the providers below, entered under **Settings** in the portal.

Pick a provider and model above the composer, type a question (or pick one of the starter questions
on the empty page), and send. The first thing to know is that
answers are grounded: when the assistant makes a claim it shows the tool calls behind it, and you can
expand any of them to see what came back.

Symbols in an answer are links. The assistant writes them as `whygraph://symbol/<qualified_name>`,
and clicking one jumps to that symbol in the Explorer - graph recentered, detail panel open.

## Providers

Chat needs **streaming tool calls**, which not every adapter supports. Four of WhyGraph's five LLM
providers can drive it:

| Provider | Chat | Key |
|---|---|---|
| `anthropic` | Yes | Anthropic API key |
| `openai` | Yes | OpenAI API key |
| `deepseek` | Yes | DeepSeek API key |
| `openrouter` | Yes | OpenRouter API key |
| `ollama` | No | - |

`ollama` is excluded because local models' tool-calling reliability varies too much to depend on. It
still works for `[analyze]` and `[rationale]`. See
[LLM providers](../reference/llm-providers.md).

Chat needs a key stored in Settings; keys in your shell do not count. With no key for any chat
provider, **New chat** is disabled and the Chat page says what to do. A new chat starts on the
provider set in Settings > Models and keys > **Chat** when it has a key, otherwise on the first
provider that has one, and the picker lists only providers with a key. An older chat whose provider
has lost its key still shows that provider, greyed out, with a note under the message box. Sending
there is refused until you switch to a provider that has a key. The model pickers are also disabled
while a monthly budget is hard-stopped.

The chat header shows what the session has cost so far ("This chat: ~$0.84") when you are allowed to
see usage, and each reply ends with its token counts ("31,500 in · 2,250 out").

!!! note "The model list may fall back"
    The picker asks your provider for its live model list. If your API key is scoped narrowly it can
    work for chat but return `401` on the models endpoint - in that case WhyGraph shows a short
    built-in list instead. That is normal operation, not an error.

### Choosing a default

Adding a project doesn't ask about chat - you pick a provider and model per session in the composer.
The `[chat]` provider and model (the **chat** override in the project's Models settings) set the
**defaults for new sessions**, and nothing more: each session records the pair it was started with, so
changing your config never rewrites an existing conversation's history or re-answers it with a different
model.

```toml
[chat]
# provider = "anthropic"      # default: the provider of [llm].model; without a key for it, the first provider that has one
# model = "claude-opus-4-7"   # default: [llm].model, then the provider's default
```

Leave `[chat]` out and new sessions follow `[llm].model`, the default for every role. If that names a
provider that cannot chat (`ollama`), chat falls back to the first provider with a stored key; naming
it in `[chat]` itself is refused.

If OpenRouter is your provider, pin a tool-capable model rather than leaving `openrouter/auto` -
automatic routing can land on a model that does not support tool calling.

### Effort

The **Effort** select sits beside the model picker above the composer. It sets how hard the model
thinks, for this chat, from the next reply on. The levels are `low`, `medium`, `high`, `xhigh` and
`max`.

**Default** uses the level set for chats under Settings > Models and keys > **Chat**, or the
provider's own default when none is set.

Each provider receives the level in its own parameter:

| Provider | Sent as |
|---|---|
| `anthropic` | `output_config.effort` |
| `openai` | `reasoning_effort` |
| `deepseek` | `reasoning_effort`, passed through as it is |
| `openrouter` | `reasoning.effort` |

Some models have no effort setting: Claude Haiku 4.5, GPT-4o and GPT-4o mini. With a level chosen, a
reply from one of them fails with an error saying the model does not take an effort level. Set
Effort to **Default** for that chat.

On Anthropic, changing the effort starts a fresh prompt cache for that reply, so the first reply after
a change costs a little more.

## Modes

The **Mode** select beside the model pickers sets how the assistant approaches a question. A mode is a
preset, not a restriction: every mode can use all twenty-three tools, and a mode only changes which ones the
assistant reaches for first and how it words the answer. Each mode also has its own starter questions
for an empty chat.

| Mode | For |
|---|---|
| **Engineer** | Code-first answers: symbols, files and lines, and why the code is the way it is. |
| **Lead** | The state of the work: what shipped, what is in flight or stuck, and who knows each area. |

You can switch a chat's mode at any time; it applies from the next reply, and each reply records the
mode it was written in. Changing it starts a fresh prompt cache for that reply, as changing the effort
does.

**Which mode a new chat starts in.** Your last-used mode in this project; if you have no chat yet, the
project's `[chat].mode` (Settings > Models and keys > **Chat** > **Mode**; see
[the configuration reference](../reference/configuration.md#section-by-section)); otherwise Engineer. There is no organization-wide
default. Modes are built into WhyGraph: a repository cannot add one or change one. A chat or a
`[chat].mode` that names a mode WhyGraph no longer has (early Onboarding and Product modes were
removed) runs as the project's default, else Engineer.

### References in your question

When you name something WhyGraph can look up - a `symbol`, a file path, `#412`, a commit SHA - it
resolves the name against the index and the project database before the model sees the question, and
passes the result along with it: which names are real, where they live, whether a pull request or issue
is open, and whether a rationale card for a symbol is already cached. A name that does not resolve is
marked as such. The block carries identifiers and states only, never a pull request title or a commit
message. It is not shown in the chat, it is kept with your message so later replies in the chat see the
same text, and **Continue** and **Regenerate** reuse it rather than looking again. The lookup has a
1.5 second budget, reads from the index only (no git, no scan, no model call), and never fails a turn:
when it cannot finish, the question goes through as you wrote it.

!!! note "WhyGraph never forces a tool call"
    The assistant is encouraged to look things up (by the mode, and by the references above), but a
    turn is never forced to call a tool. Some providers reject a forced tool choice when the model is
    thinking, and where one accepts it, it turns the thinking off for that reply. Forcing would trade
    answer quality for a guarantee, so WhyGraph measures instead: tool calls are counted by name in
    the project's [agent activity](../portal/agents.md#agent-activity).

## Sessions

Your conversations are listed in the sidebar's **Chats** section, below **All projects**, newest
activity first, each with how long ago it was last used. The 20 most recent show first; **Show more**
adds the next 20. On a phone the list is in the navigation sheet, and tapping a chat closes the sheet.

- **+** opens a new chat. Nothing is saved until you send the first message: the session is created
  then, so opening a new chat and leaving never leaves an empty "New chat" behind.
- A new session is **auto-titled** from your first message; the title shows in the list while the
  first answer is still streaming, and a chat whose answer is streaming shows a small spinner.
- The **...** menu on each chat has **Rename** (edit the title in place; Enter saves, Escape cancels)
  and **Delete** (asks first; it removes the chat's messages for good).
- Sessions are deep-linkable at `/p/<slug>/chat/<id>` - copy the URL to come back to a conversation.
- Sessions are stored in the project's WhyGraph database alongside the evidence, so they survive
  restarts and are per-repo, not global. In production each person sees only their own chats.

The Chats section appears only where you can chat: not for the Viewer role, not for a
[linked project](../portal/platform-projects.md) (its chat lives on the platform), and not for a
project that is not set up yet. When a hard-stopped budget is used up, **+** is disabled with the
reason, and earlier chats stay readable.

### Editing and regenerating

Hover a question and choose **Edit** to change it and get a new answer. Hover any reply and
choose **Regenerate** for another answer. Both keep the earlier version: a `‹ 2/3 ›` arrow beside the
message switches between the versions. A new version uses the chat's current provider, model and
effort.

**Branch from here** (on any question or reply) starts a new chat with the conversation up to and
including that message, named after the original with "(branch)", and opens it. The original chat stays
exactly as it was. Nothing is sent to the model, so it works even without a key; the new chat waits for
your next message (or **Regenerate**, if you branched from a question).

**Copy** works on any message and copies its text. For a reply, that is the answer text, never its
reasoning.

The actions and arrows are hidden while a reply is being written.

### Rating a reply

A finished reply has a thumbs up and a thumbs down beside **Regenerate**. Thumbs down opens a small
panel: pick what went wrong (Wrong, Incomplete, Not grounded, Too slow or Other) and, if you like, add a
note of up to 500 characters. Click the active thumb again to clear the rating. The rating shows again
when you reload the chat, and a **Branch from here** copy starts unrated.

Only the reply that ends a turn can be rated: not your question, and not a step in the middle of a
turn that is still calling tools. A reply that ends in an error, a stop or the budget limit can be
rated. Rating needs no key, works while another reply is being written and does not move the chat in
the session list. Ratings and notes stay in the project's own database; they are never in a log, the
usage ledger or the audit log, and only the person who started the chat reads them back.

For the developer of a local WhyGraph, `scripts/chat_signals.py` ranks the turns of a project's database
by these ratings and by what happened to them (regenerated, edited, continued, stopped, errored,
tool errors). It opens the database read-only and refuses a database with more than one chat owner
unless `--owner` is given. Its output holds your questions: do not commit it until each question is
rewritten.

### Stop

Choose **Stop** while a reply is being written to end it at once. The text written so far stays in
the chat, marked "Stopped." A tool call that is already running finishes first, so Stop can wait for
that tool.

If you send right after stopping, the chat shows "Stopping the previous reply…" and sends your
message as soon as the stopped reply has ended. A chat runs one reply at a time.

## What it can look at

Twenty-three tools across six sources. You never call these directly; they are listed so you know what
the assistant can and cannot reach.

<div class="grid cards" markdown>

-   __Code structure__ (CodeGraph)

    ---

    `search_symbols`, `get_symbol`, `get_area_outline`

-   __Evidence and rationale__ (WhyGraph)

    ---

    `get_rationale`, `get_evidence`, `get_area_history`, `find_changes`, `get_commit`, `get_pr`,
    `get_issue`, `get_repo_overview`, `list_recent_activity`, `get_work_in_flight`, `get_area_owners`,
    `compare_refs`, `get_milestones`

-   __Statistics__ (aggregate SQL)

    ---

    `run_project_stats` over commit history, `run_graph_stats` over the CodeGraph index

-   __Charts__

    ---

    `render_chart`

-   __Source tree__

    ---

    `read_file`, `list_dir`

-   __Team notes__

    ---

    `find_notes`, `propose_note`

</div>

### Team notes { #team-notes }

[Project notes](notes.md) are short facts the team confirmed: decisions, constraints, gotchas,
conventions. Two tools reach them:

- `find_notes` reads **confirmed** notes only, by path, area, symbol, a text query, or
  `project_wide`, at most ten per call. A note whose code changed since it was confirmed says so, and
  the assistant tells you. `get_rationale` carries the notes that apply to its target too.
- `propose_note` proposes one when you state a decision or ask the assistant to remember something -
  never on its own from commit messages, pull request or issue text - and at most twice in a turn
  (a third is declined, not an error).

The assistant cites a note as `note #12`. Note text is data about the project: it never changes how
the assistant behaves. The assistant's starting overview carries only a **count** of project-wide
notes; it fetches their text with `find_notes` when your question is about conventions or constraints.

A proposal appears in the chat as a **note card**, built from the saved note rather than from the
assistant's words. It shows the full text, the kind and the anchor, says that the assistant wrote it
and that everyone who can open the project can see it, and offers **Confirm** (which confirms exactly
what the card shows) and **Discard**. A project-wide proposal, or one on a top-level folder, shows
**Review** instead, which opens the Notes page, because a [wide note](notes.md#notes-that-reach-every-session)
needs another member to confirm it. If an identical proposal already exists, the card says so and
links to it. Reload the chat and the card shows the note's current status. The project's
[Overview](../portal/projects.md#notes-card) counts what is waiting for review.

Unlike a chat, which is private to you, a note - proposals included - is visible to everyone who can
open the project.

### Areas, open work and owners

If the project configures [areas](../reference/configuration.md#areas), the assistant sees them and
can narrow `find_changes` with `area` (or a path, not both) and `since_days`;
`list_recent_activity` also takes `since_days`.

- **`get_work_in_flight`** lists what is open right now: pull requests (drafts included) and issues,
  each with its idle time and a stale flag (a PR idle 7 days, an issue 30, by default; drafts are never
  stale), plus counts. Each PR also carries its review decision, who was asked to review (users and
  bots), when it got its first review, its assignees, its milestone and its diff size; each issue its
  assignees and milestone. With `area` it keeps items placed in that area: a PR by its labels, else by
  its changed files, else by its commits when they are already scanned (`area_source` says which); an
  issue by its labels only. It states `forge_data_as_of` (when GitHub was last crawled) and lists
  anything it cannot know under `not_tracked`, such as review requests to a team, deployments and
  incidents, so the assistant says "not tracked" instead of guessing. Items past the limit are counted
  in `omitted`.
- **`get_area_owners`** answers "who knows this code": for an area or a path (exactly one), each
  person's share of the commits, files touched, last touch and last activity anywhere, plus the
  CHAOSS Contributor Absence Factor (the fewest people whose shares reach half the commits) overall
  and among people active in the last 90 days. It counts default-branch commits in the window
  (365 days by default) and leaves out merges, bots and formatting or refactor sweeps;
  `Co-authored-by` trailers and CODEOWNERS are not read. This is **ownership of the code, not anyone's
  performance**.

- **`compare_refs`** answers "what lies between two fixed points": the commits one ref adds on top of
  another (`from..to`), the pull requests they belong to, the issues those closed, and a grouping by
  area. A ref is a tag name, a commit SHA (7 to 40 hex characters) or an ISO date (`YYYY-MM-DD`, the
  last default-branch commit on or before that day); leave the newer one out for the newest scanned
  commit. An unknown ref's error lists the newest tags. Use it for "what shipped in v1.4".
- **`get_milestones`** answers "how is this milestone tracking": due date, days to due, an overdue
  flag, GitHub's progress percentage and the open and closed counts of issues and pull requests, plus
  a **burndown** (open and closed items per day) for one milestone, which the assistant draws with
  `render_chart`. Items count from their creation date; when they joined the milestone is not tracked.

Area matching follows the paths you configured; directory renames are not followed (see
[Areas](../reference/configuration.md#areas)).

### Statistics are aggregate-only

The two `*_stats` tools let the assistant write its own SQL, which is why they are fenced hard. Every
query runs through a SQLite authorizer that permits **read-only aggregates** and nothing else, over an
explicit table allowlist. Results cap at **200 rows** with a **5-second** deadline.

The allowlist deliberately excludes `chat_*` (the assistant's own transcripts) and `rationale_cache` -
the assistant cannot query its own conversations, and coverage is reported through the proper tools
rather than by reading the cache table.

### Reading files

`read_file` returns at most **400 lines or 100 KB** per call, `list_dir` at most **200 entries**, and
both refuse anything under `.git/`, `.whygraph/`, or `.codegraph/`, plus `whygraph.toml` itself - it may hold real API keys. WhyGraph's own storage is reachable only through the knowledge tools,
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

Tuned under `[chat]` - imported from a repository's `whygraph.toml` when you add the project:

| Key | Default | What it bounds |
|---|---|---|
| `max_tool_rounds` | `8` | Tool rounds per user turn. On the last round the assistant is called once more with tool calls turned off, so a turn always ends in prose rather than mid-investigation. |
| `max_rationale_generations` | `2` | Uncached rationale cards one turn may generate. `0` makes the assistant cache-only. |
| `context_token_budget` | `60000` | Conversation history sent to the model. |
| `stream_timeout_sec` | `300` | How long (30-1800 seconds) the provider may stay silent mid-reply - a long think or a large prompt - before the turn fails. |
| `max_tokens` | unset | Output one model call may write, 2048-128000 tokens. Unset means 32000 on Anthropic and the provider's own default elsewhere. |
| `max_continuations` | `1` | Automatic continuations (0-3) of a reply cut off by the output limit. |

Two behaviours worth knowing:

- **Repeated calls are flagged.** From the third identical tool call in one turn (same tool, same
  arguments) the call still runs, and its result tells the model how many times it asked.
- **Lookups run side by side.** When a step asks for several plain lookups at once (symbols,
  files, commits, pull requests), they run in parallel and the cards fill in in the model's order.
  Rationale cards, evidence, area history and statistics still run one at a time.
- **A failing tool never ends a turn.** Errors come back to the model as results, so it can correct
  course and try something else. Individual results are truncated at 30 000 characters with an
  explicit marker.
- **Rationale generated inside chat uses your `[rationale]` provider**, not the chat session's. That
  keeps the cache key identical to what the Explorer and the MCP tools produce, so a card generated
  from a conversation is the same card your editor gets.

When history outgrows `context_token_budget`, WhyGraph first elides stale tool results, then drops
whole turns from the top - never splitting a tool call from its result.

### How a turn ends

Most turns end with a reply. These cases are handled for you:

- A reply cut off by the output limit is continued, up to `max_continuations` times. If it is still
  cut off, it ends with "The reply hit the output limit." and, on the latest reply, a **Continue**
  button that carries on from where it stopped. The rest is added to the same reply.
- If the model writes nothing after using tools, WhyGraph asks it once to answer from what it gathered.
  This includes the last allowed round: when that round comes back empty (even if the model tried to
  call another tool, which is dropped), it is asked once more to answer in text.
- In a long turn, the bodies of older tool results are shortened to fit `context_token_budget`, and
  the reply says "Older tool results in this turn were shortened to fit the context."
- When the provider declines to answer, the reply says "The provider declined to answer."
- A reply that ends without any text says "The model ended its turn without a reply." and offers
  **Regenerate**.

The keys `max_tokens` and `max_continuations` come from a repository's `whygraph.toml` on import, or
from the API, the same way as `max_tool_rounds`. Only **Effort** has a field in Settings.

### When a turn fails

A failed reply stays in the transcript with its reason, and the next question works normally: the
model never sees an empty, failed or stopped reply again, nor a tool call whose result never arrived,
so one bad turn cannot break the rest of the chat. A reply that ends without any text (after tool
cards, or with nothing at all) says "The model ended its turn without a reply." instead of showing
*Thinking…* forever. A reply cut off by the output limit or declined by the provider never runs a
half-written tool call.

A dropped connection or a silence longer than `stream_timeout_sec` shows as the provider being
unreachable, and when the provider answered with an error the message ends with its request id
("request id: ..."), which you can quote to its support.

### Reasoning models

DeepSeek and some OpenRouter models think before they answer and expect that reasoning sent back
while they call tools. WhyGraph does that within a turn and, for DeepSeek, across turns too. A
DeepSeek chat that has replies from before this was supported (or from another provider) runs
without thinking until those replies leave the history the model sees.

What a model reasons shows in a collapsed **Reasoning** block above that step of its reply: open it
to read along. The block stays open or closed the way you last left it (in this browser), survives a
reload, and is never part of **Copy**. On Anthropic, WhyGraph asks for summarized thinking on every
model that supports it; a model that does not is asked again without it, once. On a model where
thinking is optional (Claude Opus 4.8, for example) this turns thinking on, which uses more output
tokens per reply.

## Limits

- One project per conversation - the project whose Chat you opened.
- No login and no multi-user support in local mode; the portal is for a single user on their own machine.
- The assistant cannot edit your code. It reads, queries, and charts.
