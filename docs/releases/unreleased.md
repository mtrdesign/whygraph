# Unreleased

!!! note "Not released yet"
    This page collects what is going into the next release. It moves into a numbered release page when
    that release is cut.

## What's new

- **Agent-file overrides.** A committed `.whygraph-overrides/` folder lets a team replace, append to or
  switch off the agent files WhyGraph ships (skills, subagents, research sources, rules), and
  **Update agent files** installs the result without an **Edited** prompt. A file WhyGraph changed after
  you replaced it is flagged with the difference, and mistakes in the folder are listed as warnings
  that never stop an update. See
  [Overriding the bundled files](../portal/agents.md#agent-files-overrides). An append can also grant
  an agent more tools (`tools` / `allowed-tools` frontmatter), for example an MCP server for a research
  source, while the file keeps following WhyGraph's updates.
