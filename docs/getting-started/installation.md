# Installation

You install WhyGraph once. The **Docker install** gives you the `whygraph` command and, through it,
the [portal](../portal/index.md): one long-running server that holds all your projects. Start it with
`whygraph up` and add your repositories from the browser.

Pick the path that fits where you are.

!!! note "Only the Docker install runs the portal"
    A `pip` / `uv` install provides **headless `whygraph scan`** only - useful in CI or a plain
    checkout - not the portal, the Explorer or the Chat assistant. For those, use the Docker install.

=== "Docker (recommended)"

    The host needs **only Docker** - no Python, Node, `gh`, or CodeGraph. One command pulls the
    published image and installs the shims from inside it:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v2.0.0/scripts/install.sh | sh
    ```

    **The tag in that URL is the version.** `v2.0.0` installs 2.0.0 - no second flag to keep in
    sync. This drops a `whygraph` shim on your `PATH` (plus a `whygraph-mcp` stub that only prints a
    removal message). Most commands wrap a `docker run --rm -v "$PWD:/workspace" … ghcr.io/mtrdesign/whygraph`
    against the current repo and are ephemeral. The exception is the portal:
    [`whygraph up`](../portal/start.md) manages one named, long-lived container so the web panel and
    the MCP endpoints can outlive the command. See [Run with Docker](../deploy/docker.md) for the full
    story.

    **Install a different version** by passing it through the pipe - the URL then only decides
    *which installer* runs:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v2.0.0/scripts/install.sh | sh -s 2.0.0
    curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v2.0.0/scripts/install.sh | sh -s latest
    ```

    `WHYGRAPH_VERSION=2.0.0` does the same and wins over the argument. `WHYGRAPH_BIN_DIR` picks the
    install directory (default `~/.local/bin`), and `WHYGRAPH_IMAGE_REPO` points at a private mirror.

    Two more are read by the installed shim rather than the installer: `WHYGRAPH_IMAGE` overrides
    the image a command runs, and `WHYGRAPH_PORT` sets the portal's port when you have not chosen one
    with `whygraph up --port`.

    !!! tip "If the installer itself misbehaves"
        Swap the tag for `main` - `…/whygraph/main/scripts/install.sh` - to get the newest
        installer while still installing the last published release. A per-tag script is frozen at
        that tag, so this is the escape hatch for an installer bug.

    !!! warning "A mistyped tag fails silently"
        `curl -f` prints nothing on a 404, so `curl … | sh` reads an empty script and **exits 0
        without installing anything** - the failure mode every `curl | sh` installer shares. When
        you want a visible failure (or want to read the script first), download it separately:

        ```bash
        curl -fsSL -o install.sh https://raw.githubusercontent.com/mtrdesign/whygraph/v2.0.0/scripts/install.sh
        sh install.sh
        ```

    **No `curl`, air-gapped, or CI?** Run the in-image generator directly - it is what the script
    above delegates to, and dropping the pipe prints exactly what would be written:

    ```bash
    docker run --rm ghcr.io/mtrdesign/whygraph:2.0.0 whygraph install | sh
    ```

=== "PyPI"

    Headless `scan` only - no portal.

    ```bash
    uv tool install whygraph        # or: pipx install whygraph
    ```

    !!! warning "Not yet published"
        There's no PyPI release job yet, so this won't resolve. Use the Docker, GitHub, or
        local-checkout paths instead.

=== "GitHub"

    Headless `scan` only - no portal. Install straight from the repo - latest `main`, a feature branch,
    or a tag:

    ```bash
    # Latest from main:
    uv tool install "git+https://github.com/mtrdesign/whygraph.git"

    # A specific branch:
    uv tool install "git+https://github.com/mtrdesign/whygraph.git@feature/some-branch"

    # A specific tag (once tagged):
    uv tool install "git+https://github.com/mtrdesign/whygraph.git@v2.0.0"
    ```

    Re-running upgrades in place. To switch refs, add `--force`. `pipx` accepts the same URLs.

=== "Local checkout"

    For contributors who want their edits to show up immediately. This gives the `whygraph` command
    for development; run the portal from a checkout with `make dev` (see
    [Develop the UI](../guide/playground.md#develop-the-ui)).

    ```bash
    git clone https://github.com/mtrdesign/whygraph.git
    uv tool install --editable ./whygraph
    ```

    `--editable` skips the reinstall on every change.

## Verify

```bash
whygraph version
```

With the Docker shim, this reports the version baked into the image the shim runs - the version you
pinned, not something read from the host.

Next: [start the portal and add a repo.](quickstart.md)
