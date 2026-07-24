# WhyGraph scanning-service runtime image.
#
# A single self-contained image that carries every dependency the scan
# side needs: Python + WhyGraph, `git`, the GitHub CLI (`gh`), and Node +
# the pinned CodeGraph CLI. The `whygraph` host shim (scripts/install.sh)
# runs this image ephemerally against the current repo:
#
#   docker run --rm -v "$PWD:/workspace" -w /workspace IMAGE whygraph <args>
#
# so each command is a fresh process that reads that repo's own
# whygraph.toml, .whygraph/ and .codegraph/. Because CodeGraph is
# installed *in this image*, the scan's CodeGraph refresh runs the local
# `codegraph` binary directly — no docker-in-docker, no host Docker socket.
#
# Build (context = repo root):
#   docker build -f docker/whygraph/Dockerfile -t whygraph .

# syntax=docker/dockerfile:1.7

# --- Playground build stage -----------------------------------------------
# Builds the Explorer SPA (src/playground/) into a static bundle that the final
# stage copies into the wheel. `--platform=$BUILDPLATFORM` pins this to the
# native builder: JS output is arch-independent, so we avoid QEMU emulating
# the npm build once per target arch (the multi-arch gotcha).
FROM --platform=$BUILDPLATFORM node:22-slim AS playground-build
WORKDIR /playground
COPY src/playground/package.json src/playground/package-lock.json ./
RUN npm ci
COPY src/playground/ ./
# Override the outDir to a stage-local `dist` (the checked-in vite.config.ts
# writes straight into the package for local `make playground`); the final stage
# COPYs it to the packaged location.
RUN npm run build -- --outDir dist --emptyOutDir

# --- Runtime stage --------------------------------------------------------
FROM python:3.12-slim

# Pinned via build arg so the version can be advanced without editing the
# body. Defaults to `latest` for local convenience; CI/publish should pass
# an explicit pin.
ARG CODEGRAPH_VERSION=latest
ARG NODE_MAJOR=22

# System deps:
#   git            — every WhyGraph command crawls git history
#   ca-certificates, curl, gnupg — fetch the gh + NodeSource apt repos
#   libgomp1       — OpenMP runtime required by scikit-learn at import time
# Then the GitHub CLI (apt repo) and Node (NodeSource) + the CodeGraph CLI.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg libgomp1; \
    # GitHub CLI
    mkdir -p -m 755 /etc/apt/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    # Node (NodeSource)
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh nodejs; \
    # CodeGraph CLI (read directly by WhyGraph; indexed locally during scans)
    npm install -g "@colbymchenry/codegraph@${CODEGRAPH_VERSION}"; \
    npm cache clean --force; \
    apt-get purge -y --auto-remove gnupg; \
    rm -rf /var/lib/apt/lists/*

# WhyGraph's own version, baked so `whygraph install` can pin the generated
# shims to a concrete release even when the image is pulled via `:latest`. The
# publish workflow passes the release tag; `latest` is the local-build default.
# Declared here (not with the top ARGs) so bumping it never busts the
# apt/node/codegraph layers above.
ARG WHYGRAPH_VERSION=latest
ENV WHYGRAPH_VERSION=${WHYGRAPH_VERSION}

# Install WhyGraph itself so the `whygraph` and `whygraph-mcp` console
# scripts land on PATH. Copy the build inputs hatchling needs and install.
WORKDIR /opt/whygraph
COPY pyproject.toml ./
COPY hatch_build.py ./
COPY src ./src
# The pre-built SPA bundle must exist under src/ BEFORE `pip install .` so
# hatchling packs it into the wheel. Copied from the playground stage, which
# means the hatch build hook (§9.3) finds static/ already populated and no-ops
# — the image build never runs npm.
COPY --from=playground-build /playground/dist ./src/whygraph/serve/static
RUN pip install --no-cache-dir .

# Image-only shortcut so `docker run IMAGE install | sh` works without an
# ENTRYPOINT — an entrypoint of `whygraph` would break `docker run IMAGE
# codegraph …` (services/codegraph/bootstrap.py) and the whygraph-mcp shim.
# Deliberately NOT a pyproject [project.scripts] entry, so a native
# `uv tool install` never drops a bare `install` on a user's PATH (it would
# shadow coreutils `install`). Added last so no earlier build step depends on
# coreutils `install`.
RUN printf '#!/bin/sh\nexec whygraph install "$@"\n' > /usr/local/bin/install \
    && chmod +x /usr/local/bin/install

# The host shim bind-mounts the current repo here and sets the command
# (e.g. `whygraph scan`). A bare `docker run IMAGE` prints CLI help.
WORKDIR /workspace
CMD ["whygraph", "--help"]
