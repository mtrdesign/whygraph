# User Guide

You've added a repo to the portal and connected an editor. This guide explains how the pieces fit
together - and how to get the most out of each.

The flow is simple. The portal **scans** a repo to build the evidence database and refresh the CodeGraph
index. Your editor connects to the project's **MCP endpoint** on the portal, which reads that database.
As you work, the endpoint's **tools, resources, and prompts** answer "why does this code exist?" from
history.

Start with the concepts, then dig into whichever piece you need.

<div class="grid cards" markdown>

-   :material-lightbulb-on:{ .lg .middle } __Concepts__

    ---

    Evidence vs rationale, and how WhyGraph splits work with CodeGraph.

    [:octicons-arrow-right-24: Concepts](concepts.md)

-   :material-radar:{ .lg .middle } __Scanning your repo__

    ---

    The four scan phases, every flag, branch membership, and the keep-fresh git hooks.

    [:octicons-arrow-right-24: Scanning](scanning.md)

-   :material-application-edit:{ .lg .middle } __Wiring your editor__

    ---

    The agents the portal connects: Claude Code, Cursor, VS Code, and Codex.

    [:octicons-arrow-right-24: Editors](editors.md)

-   :material-connection:{ .lg .middle } __Using WhyGraph (MCP)__

    ---

    How an agent calls the tools, resources, and prompts mid-task.

    [:octicons-arrow-right-24: MCP usage](mcp-usage.md)

-   :material-graph-outline:{ .lg .middle } __The Explorer__

    ---

    The portal's web view of the graph, evidence, and rationale.

    [:octicons-arrow-right-24: Explorer](playground.md)

-   :material-chat-processing-outline:{ .lg .middle } __The Chat assistant__

    ---

    Ask questions in English; it calls WhyGraph's tools and charts the answers.

    [:octicons-arrow-right-24: Chat](chat.md)

</div>
