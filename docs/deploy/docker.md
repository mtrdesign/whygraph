# Run with Docker

Don't want Python, Node, `gh`, and CodeGraph on your machine? WhyGraph ships as a self-contained
image. Your host needs **only Docker**. One command installs the `whygraph` shim from inside the image;
`whygraph up` then starts the [portal](../portal/index.md) in it.

```bash
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v1.1.2/scripts/install.sh | sh

whygraph up          # start the portal (one background container, loopback only)
```

Then open <http://localhost:8765>, share the folder that holds your repos, and add them from the
Projects page. See the [Quickstart](../getting-started/quickstart.md).

**The tag in the URL picks the version** - `v1.1.2` installs 1.1.2. To install a different release
with that same installer, pass it through the pipe: `… | sh -s latest`. Full override list and the
no-`curl` alternative are in [Installation](../getting-started/installation.md).

## How the shim works

The fetched script is a thin bootstrapper: it checks Docker is present and running, pulls the pinned
image, then asks the image to generate the shims - `docker run --rm IMAGE whygraph install` prints
them to stdout and the script executes that. Shim bodies therefore live in WhyGraph's own tested
code, not in the shell script, and every failure (no Docker, dead daemon, unknown version) exits
non-zero with a message instead of quietly installing nothing.

The result is a `whygraph` shim on your `PATH`, plus a `whygraph-mcp` stub that only prints a message
that the stdio server was removed. The shim does two different things:

- **The portal verbs** - `up`, `down`, `status`, `logs` and `folders` - manage one **named,
  long-lived** container, `whygraph-portal`. This is the one exception to "ephemeral per command".
- **Every other command** runs the image against the current directory and exits:

```bash
exec docker run --rm -i $tty \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD:/workspace" -w /workspace \
    -e GH_TOKEN -e GITHUB_TOKEN \
    -e ANTHROPIC_API_KEY -e OPENAI_API_KEY -e DEEPSEEK_API_KEY \
    -e OPENROUTER_API_KEY \
    "$IMAGE" whygraph "$@"
```

- **Everything's in the image** - Python and WhyGraph, `git`, the GitHub CLI, and Node with the
  CodeGraph CLI. CodeGraph indexes from the in-image binary, so there's no docker-in-docker.
- **Files come back as yours.** `--user "$(id -u):$(id -g)"` is what does it: generated files aren't
  root-owned and git sees matching ownership.
- **Only the repo you stand in is visible** to an ephemeral command. The portal sees only the
  [folders you share](../portal/shared-folders.md).

## The portal container

`whygraph up` starts `whygraph-portal` detached, with `--restart unless-stopped`, published to
the loopback interface only and attached to its own Docker network. Your shared folders are mounted at the same
path they have on the host, and the data directory (`~/.local/share/whygraph`) is mounted at `/data`.
The container runs as your user, so files it writes come back owned by you.

It is recreated automatically when the shared folders, the port or the image change, and stopped
cleanly by `whygraph down` (it gives a running scan time to be recorded as interrupted). The host
commands are in [Start the portal](../portal/start.md).

## Credentials

The portal container is started with **none of your environment**. Keys and tokens are entered in the
portal's Settings and stored encrypted in its database - see the
[security model](../portal/security.md#keys-and-tokens).

The ephemeral shim path still forwards `GH_TOKEN` / `GITHUB_TOKEN` and the four provider API keys, for
headless `whygraph scan` in a repository the portal does not manage (a CI job, say).

!!! warning "Never bake a token into the image"
    Pass credentials at run time, never at build time.

## Agents

Agents do not launch anything in Docker any more. They connect over HTTP to the portal's per-project
endpoint, which the portal writes into each agent's config when you initialize the project. See
[Connecting agents](../portal/agents.md).

## Build the image yourself

Building locally instead of pulling - say, while developing:

```bash
docker build -f docker/whygraph/Dockerfile -t whygraph:latest .
WHYGRAPH_IMAGE=whygraph:latest whygraph up
```

`WHYGRAPH_IMAGE` overrides the image the shim runs, so you can test a local build without touching the
install. The image also carries the built Explorer bundle, so a local build serves the portal UI too.

!!! note "A local build reports itself as `latest`"
    The release version is baked in at build time. Building yourself bakes `latest`, which is what
    `whygraph version` and the installer read back - expected, not a bug.
