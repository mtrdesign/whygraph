# Quickstart

You've [installed WhyGraph](installation.md). The happy path is three moves: **install, `whygraph up`,
browser**. There is no per-repo command to run first.

## 1. Start the portal

```bash
whygraph up --add-folder ~/Work    # the folder that holds your repos
```

This starts the WhyGraph portal in the background, `127.0.0.1` only, with its database in a second
container, and shares that folder with it. Drop `--add-folder` to start without sharing anything yet; you can share later. See
[Start the portal](../portal/start.md) and [Shared folders](../portal/shared-folders.md).

## 2. Open it in the browser

Open <http://127.0.0.1:8765>. The first time, enter your name on the Welcome screen. You land on an
**Projects** page, which starts with a short checklist: add an LLM key, add a project, connect your
agent. Each step ticks itself off; **Dismiss** hides the list and **Getting started** brings it back.

## 3. Add a project

Choose **New project**, pick a repository from your shared folders, and follow the three steps:

1. **Source** - the repository.
2. **Set up** - review the preview and choose which agents to connect. Set provider keys once under
   **Settings** and every project inherits them.
3. **Configure** - the first scan starts on its own: it reads git history and refreshes the CodeGraph
   index, with live progress, and it never calls an LLM, so it is free and fast. This step also shows
   the estimate for the optional descriptions.

!!! warning "Enter your keys in the portal"
    `ANTHROPIC_API_KEY`, `GH_TOKEN` and the other credentials in your shell environment do **not**
    reach WhyGraph. Enter them under Settings. See [Upgrading from 1.x](../portal/upgrading.md#credentials-from-your-shell-environment).

[Adding projects](../portal/projects.md) covers each step in detail.

## 4. Browse, then ask your editor

The project opens in the [Explorer](../guide/playground.md): the code graph with rationale and
evidence side by side, and a [Chat assistant](../guide/chat.md) that answers questions by calling
WhyGraph's tools.

Your agent connected during Set up. Approve the `whygraph` server when it asks (Claude Code) or trust
the project (Codex), then ask why a function exists and WhyGraph answers from history. See
[Connecting agents](../portal/agents.md).

## Descriptions when you want them

After the first scan, the project offers **Describe now**, with the commit count, the model and a cost
estimate, to write the LLM description for each commit. Skip it and descriptions backfill on demand.
What you spend, per project and model, is tracked under [Usage & cost](../portal/usage.md).
See [Scanning your repo](../guide/scanning.md).

## Where to next

<div class="grid cards" markdown>

-   :material-lightbulb-on:{ .lg .middle } __Concepts__

    ---

    Evidence, rationale cards, and the CodeGraph split.

    [:octicons-arrow-right-24: Concepts](../guide/concepts.md)

-   :material-connection:{ .lg .middle } __Using WhyGraph__

    ---

    How an agent calls the tools mid-task.

    [:octicons-arrow-right-24: MCP usage](../guide/mcp-usage.md)

-   :material-shield-lock-outline:{ .lg .middle } __Security model__

    ---

    What the portal exposes, and what it never does.

    [:octicons-arrow-right-24: Security](../portal/security.md)

</div>
