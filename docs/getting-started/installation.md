# Installation

WhyGraph follows a one-global-install, use-anywhere model - like `npx`, but for Python. You install
the package once; that puts `whygraph` and `whygraph-mcp` on your `PATH`. Then
`whygraph init --agent <name>` wires each project so its editor can launch the MCP server.

Pick the path that fits where you are.

=== "Docker (recommended)"

    The host needs **only Docker** - no Python, Node, `gh`, or CodeGraph. A tiny shim runs everything
    inside one published image.

    ```bash
    curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/main/scripts/install.sh | sh
    ```

    This drops `whygraph` and `whygraph-mcp` shims on your `PATH`. Each wraps a
    `docker run --rm -v "$PWD:/workspace" … ghcr.io/mtrdesign/whygraph` against the current repo. The
    container is ephemeral per command. See [Run with Docker](../deploy/docker.md) for the full story.

=== "PyPI"

    ```bash
    uv tool install whygraph        # or: pipx install whygraph
    ```

    !!! warning "Not yet published"
        WhyGraph isn't on PyPI yet. Use the GitHub or local-checkout paths until v1 ships.

=== "GitHub"

    Install straight from the repo - latest `main`, a feature branch, or a tag:

    ```bash
    # Latest from main:
    uv tool install "git+https://github.com/mtrdesign/whygraph.git"

    # A specific branch:
    uv tool install "git+https://github.com/mtrdesign/whygraph.git@feature/some-branch"

    # A specific tag (once tagged):
    uv tool install "git+https://github.com/mtrdesign/whygraph.git@v1.0.0"
    ```

    Re-running upgrades in place. To switch refs, add `--force`. `pipx` accepts the same URLs.

=== "Local checkout"

    For contributors who want their edits to show up immediately:

    ```bash
    git clone https://github.com/mtrdesign/whygraph.git
    uv tool install --editable ./whygraph
    ```

    `--editable` skips the reinstall on every change.

## Verify

```bash
whygraph version
which whygraph-mcp
```

Both should resolve to your global tool install. With the Docker shim, `which whygraph-mcp` points at
the shim script on your `PATH`.

Next: [scan a repo and wire your editor.](quickstart.md)
