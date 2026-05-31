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

# Install WhyGraph itself so the `whygraph` and `whygraph-mcp` console
# scripts land on PATH. Copy the build inputs hatchling needs and install.
WORKDIR /opt/whygraph
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# The host shim bind-mounts the current repo here and sets the command
# (e.g. `whygraph scan`). A bare `docker run IMAGE` prints CLI help.
WORKDIR /workspace
CMD ["whygraph", "--help"]
