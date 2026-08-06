# Installation

WhyGraph follows a one-global-install, use-anywhere model - like `npx`, but for Python. You install
the package once; that puts `whygraph` and `whygraph-mcp` on your `PATH`. Then
`whygraph init --agent <name>` wires each project so its editor can launch the MCP server.

Pick the path that fits where you are.

=== "Docker (recommended)"

    The host needs **only Docker** - no Python, Node, `gh`, or CodeGraph. One command pulls the
    published image and installs the shims from inside it:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v1.1.1/scripts/install.sh | sh
    ```

    **The tag in that URL is the version.** `v1.1.1` installs 1.1.1 - no second flag to keep in
    sync. This drops `whygraph` and `whygraph-mcp` shims on your `PATH`; each wraps a
    `docker run --rm -v "$PWD:/workspace" … ghcr.io/mtrdesign/whygraph` against the current repo,
    and the container is ephemeral per command - the one exception being
    [`whygraph serve`](../guide/playground.md), which manages a named background container so the
    web panel can outlive the command. See [Run with Docker](../deploy/docker.md) for the full story.

    **Install a different version** by passing it through the pipe - the URL then only decides
    *which installer* runs:

    ```bash
    curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v1.1.1/scripts/install.sh | sh -s 1.1.1
    curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v1.1.1/scripts/install.sh | sh -s latest
    ```

    `WHYGRAPH_VERSION=1.1.1` does the same and wins over the argument. `WHYGRAPH_BIN_DIR` picks the
    install directory (default `~/.local/bin`), and `WHYGRAPH_IMAGE_REPO` points at a private mirror.

    Two more are read by the installed shims rather than the installer: `WHYGRAPH_IMAGE` overrides
    the image a single command runs, and `WHYGRAPH_PORT` sets the port for
    [`whygraph serve`](../guide/playground.md).

    !!! tip "If the installer itself misbehaves"
        Swap the tag for `main` - `…/whygraph/main/scripts/install.sh` - to get the newest
        installer while still installing the last published release. A per-tag script is frozen at
        that tag, so this is the escape hatch for an installer bug.

    !!! warning "A mistyped tag fails silently"
        `curl -f` prints nothing on a 404, so `curl … | sh` reads an empty script and **exits 0
        without installing anything** - the failure mode every `curl | sh` installer shares. When
        you want a visible failure (or want to read the script first), download it separately:

        ```bash
        curl -fsSL -o install.sh https://raw.githubusercontent.com/mtrdesign/whygraph/v1.1.1/scripts/install.sh
        sh install.sh
        ```

    **No `curl`, air-gapped, or CI?** Run the in-image generator directly - it is what the script
    above delegates to, and dropping the pipe prints exactly what would be written:

    ```bash
    docker run --rm ghcr.io/mtrdesign/whygraph:1.1.1 whygraph install | sh
    ```

=== "PyPI"

    ```bash
    uv tool install whygraph        # or: pipx install whygraph
    ```

    !!! warning "Not yet published"
        There's no PyPI release job yet, so this won't resolve. Use the Docker, GitHub, or
        local-checkout paths instead.

=== "GitHub"

    Install straight from the repo - latest `main`, a feature branch, or a tag:

    ```bash
    # Latest from main:
    uv tool install "git+https://github.com/mtrdesign/whygraph.git"

    # A specific branch:
    uv tool install "git+https://github.com/mtrdesign/whygraph.git@feature/some-branch"

    # A specific tag (once tagged):
    uv tool install "git+https://github.com/mtrdesign/whygraph.git@v1.1.1"
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
the shim script on your `PATH`, and `whygraph version` reports the version baked into the image the
shim runs - which is the version you pinned, not something read from the host.

Next: [scan a repo and wire your editor.](quickstart.md)
