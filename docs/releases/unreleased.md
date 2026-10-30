# Unreleased

!!! note "Not released yet"
    This page collects what is going into the next release. It moves into a numbered release page
    when that release is cut.

## What's new

- **Rate a Chat reply.** A finished reply has a thumbs up and a thumbs down; thumbs down takes a reason
  and an optional note. See [Rating a reply](../guide/chat.md#rating-a-reply).
- **Tool outcomes and timings in Agent activity.** The Overview now shows, per kind of call, the
  error rate and the mean time per call, and flags calls that ended in an internal error. Counting
  moved to when a call ends. Still counts only. See [Agent activity](../portal/agents.md#agent-activity).
- **A reply after the last tool round.** If the final allowed round comes back empty, Chat asks the
  model once to answer in text. See [How a turn ends](../guide/chat.md#how-a-turn-ends).
- **A failed tool result stays failed.** The error flag of a tool result is kept when a chat is
  reloaded, branched or shortened, and is sent to Anthropic.
- **Pull request comments are capped.** `whygraph://pr/{number}` returns the first 50 comments, each
  cut to 4,000 characters, with an `omitted` key when anything was cut. See the
  [MCP reference](../reference/mcp.md#resources).
- **Chat signals script.** `scripts/chat_signals.py` ranks the turns of a local project database by
  ratings and by what happened to them.

## Fixes

- **Claude Sonnet 5.5 cache reads are priced right.** The bundled price table charged cache reads at
  $0.20 per million tokens; it is $0.10. Usage recorded from now on is estimated at the new price;
  rows already written keep their cost, and an organization's own price override is unchanged. See
  [Prices and overrides](../portal/usage.md#prices-and-overrides).
- **Rolling back after this release.** This release adds columns to each project's WhyGraph
  database and to the portal's database. An older image cannot open an upgraded project database: to
  go back, restore each project's `backups/whygraph-<previous revision>.db`, which the portal writes
  beside its `whygraph.db` before upgrading it.

## For developers

- **A sharper Chat eval.** `scripts/eval_tool_choice.py` reports the cache split, uncached input and
  side spend per turn; runs multi-turn cases as one session; fails an answer that cites a commit or
  pull request the repository does not have, or calls a tool that does not exist; and adds a held-out
  case set, an offline replay of request sizes on recorded sessions, and blind baseline-versus-candidate
  packets with an optional second judge model.
