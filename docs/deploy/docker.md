# Run with Docker

Don't want Python, Node, `gh`, and CodeGraph on your machine? WhyGraph ships as a self-contained
image. Your host needs **only Docker**. Install a tiny shim, then it's the same `init` and `scan` as a
native install.

```bash
curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/main/scripts/install.sh | sh

cd your-repo
whygraph init      # bootstrap the WhyGraph DB + write config
whygraph scan      # crawl history + refresh CodeGraph + LLM descriptions
```

## How the shim works

`install.sh` drops `whygraph` and `whygraph-mcp` shims on your `PATH`. Each one runs the published
image against the current directory:

```bash
docker run --rm -v "$PWD:/workspace" -w /workspace ghcr.io/mtrdesign/whygraph whygraph "$@"
```

The container is **ephemeral per command** — no compose, no `docker exec`, nothing to start or stop.
Each invocation is a fresh process against the repo you're standing in.

- **Everything's in the image** — Python and WhyGraph, `git`, the GitHub CLI, and Node with the
  CodeGraph CLI. CodeGraph indexes from the in-image binary, so there's no docker-in-docker.
- **Per-project config just works.** Each command reads the current repo's own `whygraph.toml`,
  `.whygraph/`, and `.codegraph/`.
- **Files come back as yours.** The shim runs as your host user, so generated files aren't
  root-owned and git sees matching ownership.

## Credentials

The shim passes your environment through. A GitHub token goes in `[scan].token` of the repo's
`whygraph.toml` (gitignored), and the shim also forwards `GH_TOKEN` / `GITHUB_TOKEN` plus
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` from your environment.

!!! warning "Never bake a token into the image"
    Pass credentials at run time, never at build time. The repo's gitignored `whygraph.toml` is the
    right home for a pinned token.

## Wire your editor, still only Docker

The MCP server is containerized too. `install.sh` drops a `whygraph-mcp` shim alongside `whygraph`, so
there's nothing extra to install. Wire your editor from inside the repo:

```bash
whygraph init --agent claude     # writes .mcp.json (also: --agent cursor / vscode / codex)
```

The generated config launches `whygraph-mcp` by bare command name. Your editor resolves it to the
shim, which starts a per-session container speaking MCP over stdio. It reads the repo's `.whygraph/`
and `.codegraph/` over the same `/workspace` mount the scan writes to — so the editor and the scan
share one source of truth on disk.

## Build the image yourself

Building locally instead of pulling — say, while developing:

```bash
docker build -f docker/whygraph/Dockerfile -t whygraph:latest .
WHYGRAPH_IMAGE=whygraph:latest whygraph scan
```

`WHYGRAPH_IMAGE` overrides the image the shim runs, so you can test a local build without touching the
install.
