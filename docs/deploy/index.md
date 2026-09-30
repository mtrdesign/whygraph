# Docker & Self-Hosting

WhyGraph ships as one self-contained image, and the **portal** runs from it. The same image serves two
kinds of driver - you at a browser and an editor, or an application over MCP.

<div class="grid cards" markdown>

-   :material-laptop:{ .lg .middle } __As a local dev tool__

    ---

    Install the Docker shim, start the portal with `whygraph up`, and add your repos from the browser -
    no Python or Node on the host. This is the default install.

    [:octicons-arrow-right-24: Run with Docker](docker.md)

-   :material-server-network:{ .lg .middle } __As a service__

    ---

    The portal container is a long-lived service with an HTTP MCP endpoint per project that real
    applications - not just editors - connect to for git-based analysis of a repo.

    [:octicons-arrow-right-24: WhyGraph as a service](service.md)

</div>

Most people start with the local tool. Reach for the service model when you're building an
application that needs the *why* behind code - a review bot, an onboarding assistant, an internal
portal.
