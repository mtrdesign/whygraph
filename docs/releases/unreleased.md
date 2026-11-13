# Unreleased

!!! note "Not released yet"
    These changes are on `main` and ship in the next release.

## Fixes

- **DeepSeek answers no longer show raw tool-call markup in Chat.** DeepSeek models sometimes write a
  tool call as text (`<｜｜DSML｜｜ invoke ...>`) instead of calling the tool. Chat used to show that
  markup as the answer. It now runs the call the markup describes and keeps the markup out of the
  answer. This also covers DeepSeek models served through OpenRouter. Markup that cannot be read is
  still shown, as before.

## Changes

- **Rationale cards keep the source's hedges.** When a commit, PR or issue calls a risk possible, a
  new card keeps it possible and names the source, for example "PR #12 notes a possible race". The
  card still adds no hedges of its own. Cards already cached are unchanged until they are generated
  again.
- **Plainer WhyGraph MCP tool descriptions.** The tool descriptions and server instructions an agent
  reads are split into shorter sentences. The `whygraph_propose_note` line now says what the tool does.
  Nothing to update: a connected agent reads the new text on its next session.
