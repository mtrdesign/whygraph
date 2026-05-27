# `whygraph-codegraph` Docker image

Vendored runtime for CodeGraph (https://github.com/colbymchenry/codegraph),
consumed by `whygraph init`. The image bakes a pinned release of the
`@colbymchenry/codegraph` npm package onto a `node:22-slim` base so
contributors don't need Node 22 — or any host Node install — to bootstrap
`.codegraph/codegraph.db` in their project.

## Published tags

`ghcr.io/mtrdesign/whygraph-codegraph:<tag>` — built and pushed by
`.github/workflows/publish-codegraph-image.yml`. Tags:

- `:latest` — head of `main`. Tracks the most recent change in this directory.
- `:sha-<short>` — every push that builds the image. Use this when you need
  to pin to an exact build, e.g. from CI.
- `:vX.Y.Z` — set via `workflow_dispatch` when freezing a specific
  upstream CodeGraph version into the image.

The `--codegraph-image` flag on `whygraph init` accepts any of these.

## Rebuild locally

```bash
# Build for the host architecture only — fast iteration loop.
docker build -t whygraph-codegraph:dev docker/codegraph/

# Pin a specific upstream codegraph npm version.
docker build \
    --build-arg CODEGRAPH_VERSION=1.2.3 \
    -t whygraph-codegraph:dev \
    docker/codegraph/

# Run against the current repo, same way `whygraph init` does.
docker run --rm -it \
    --user $(id -u):$(id -g) \
    -v "$(pwd)":/workspace \
    -w /workspace \
    whygraph-codegraph:dev init -i
```

## Why this image exists at all

CodeGraph is a Node tool; before this image, the documented bootstrap was
"install Node ≥ 22 via nvm, then `npm i -g @colbymchenry/codegraph`,
then `codegraph init -i`". On a laptop whose default `node` is older,
that's three friction points. The image collapses them into one: have
Docker, run `whygraph init`.
