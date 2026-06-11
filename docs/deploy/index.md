# Docker & Self-Hosting

WhyGraph ships as a self-contained image, so there are two ways to run it. Both use the same image;
they differ in who's driving.

<div class="grid cards" markdown>

-   :material-laptop:{ .lg .middle } __As a local dev tool__

    ---

    Install the Docker shim, then `init` and `scan` your repos and wire your editor - no Python or
    Node on the host. This is the default install.

    [:octicons-arrow-right-24: Run with Docker](docker.md)

-   :material-server-network:{ .lg .middle } __As a service__

    ---

    A containerized `whygraph-mcp` endpoint that real applications - not just editors - connect to
    for git-based analysis of a target repo.

    [:octicons-arrow-right-24: WhyGraph as a service](service.md)

</div>

Most people start with the local tool. Reach for the service model when you're building an
application that needs the *why* behind code - a review bot, an onboarding assistant, an internal
portal.
