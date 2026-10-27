# Installation

You install WhyGraph once. The **Docker install** gives you the `whygraph` command and, through it,
the [portal](../portal/index.md): one long-running server that holds all your projects. Start it with
`whygraph up` and add your repositories from the browser. Docker is the only way to install it.

The host needs **only Docker** - no Python, Node, `gh`, or CodeGraph. One command pulls the
published image and installs the shims from inside it:

```bash
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v2.2.1/scripts/install.sh | sh
```

**The tag in that URL is the version.** `v2.2.1` installs 2.2.1 - no second flag to keep in
sync. This drops a `whygraph` shim on your `PATH` (plus a `whygraph-mcp` stub that only prints a
removal message). The `version` and `install` commands wrap a `docker run --rm -v "$PWD:/workspace" … ghcr.io/mtrdesign/whygraph`
against the current repo and are ephemeral. The exception is the portal:
[`whygraph up`](../portal/start.md) manages two named, long-lived containers - the portal and its
database - so the web panel and the MCP endpoints can outlive the command. See [Run with Docker](../deploy/docker.md) for the full
story.

**Install a different version** by passing it through the pipe - the URL then only decides
*which installer* runs:

```bash
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v2.2.1/scripts/install.sh | sh -s 2.2.1
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v2.2.1/scripts/install.sh | sh -s latest
```

`WHYGRAPH_VERSION=2.2.1` does the same and wins over the argument. `WHYGRAPH_BIN_DIR` picks the
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
    curl -fsSL -o install.sh https://raw.githubusercontent.com/mtrdesign/whygraph/v2.2.1/scripts/install.sh
    sh install.sh
    ```

**No `curl`, air-gapped, or CI?** Run the in-image generator directly - it is what the script
above delegates to, and dropping the pipe prints exactly what would be written:

```bash
docker run --rm ghcr.io/mtrdesign/whygraph:2.2.1 whygraph install | sh
```

## Verify

```bash
whygraph version
```

With the Docker shim, this reports the version baked into the image the shim runs - the version you
pinned, not something read from the host.

Next: [start the portal and add a repo.](quickstart.md)
