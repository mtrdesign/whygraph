# Vendored CodeGraph runtime image consumed by `whygraph init`.
#
# WhyGraph reads CodeGraph's SQLite directly but doesn't ship CodeGraph
# itself. Rather than make every contributor install Node 22 + the
# `@colbymchenry/codegraph` npm package on their host, we bake the right
# Node + a pinned CodeGraph release into this image. `whygraph init`
# runs it with the project root bind-mounted to /workspace, then exits
# with `.codegraph/codegraph.db` populated.
#
# Debian-slim (glibc) rather than alpine (musl) because the upstream
# install path bundles native binaries; sticking with glibc avoids the
# musl/glibc dynamic-linker mismatches that can surface at runtime.

# syntax=docker/dockerfile:1.7

FROM node:22-slim

# Codegraph walks the working tree and consults .gitignore + git
# metadata while indexing. ca-certificates is needed by npm itself for
# the install step.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pinned via build arg so the publish workflow can advance the version
# without editing the Dockerfile body. Defaults to `latest` for local
# `docker build` convenience; the CI build always passes an explicit pin.
ARG CODEGRAPH_VERSION=latest
RUN npm install -g @colbymchenry/codegraph@${CODEGRAPH_VERSION}

# Bind-mount target — the calling user mounts their repo root here.
WORKDIR /workspace

# `docker run image init -i` runs `codegraph init -i`. Override with
# --entrypoint sh if you need to inspect the image.
ENTRYPOINT ["codegraph"]
