#!/usr/bin/env sh
# WhyGraph installer. Writes the `whygraph` and `whygraph-mcp` shims onto your
# PATH; each runs the WhyGraph container ephemerally against the current repo.
#
#   curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v1.1.0/scripts/install.sh | sh
#
# The tag in that URL is the version: DEFAULT_VERSION below matches it, and CI
# fails a release whose tag disagrees. Override with an argument or the env:
#   … | sh -s 1.1.0        … | sh -s latest        WHYGRAPH_VERSION=1.1.0 … | sh
#
# Other env: WHYGRAPH_BIN_DIR (default ~/.local/bin, read by the generated
# installer), WHYGRAPH_IMAGE_REPO (private mirrors).
#
# Every statement lives in a function and `main "$@"` is last, so a truncated
# download defines functions and never runs anything.
set -eu

DEFAULT_VERSION="1.1.0"        # the release that first ships this file; gated by CI.
IMAGE_REPO="${WHYGRAPH_IMAGE_REPO:-ghcr.io/mtrdesign/whygraph}"
RELEASES_URL="https://github.com/mtrdesign/whygraph/releases"

die() { echo "whygraph install: $*" >&2; exit 1; }
info() { echo "$*" >&2; }          # keep all human output off stdout

require_docker() {
    command -v docker >/dev/null 2>&1 || die \
        "docker not found on PATH. Install Docker Desktop (macOS/Windows) or
docker-ce (Linux): https://docs.docker.com/get-docker/"
    docker info >/dev/null 2>&1 || die \
        "the Docker daemon is not reachable — start Docker and re-run."
}

# Best-effort read of the version baked into the image, so what actually gets
# installed is visible even when the caller passed `-s latest`.
resolved_version() {
    docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
        | while IFS= read -r line; do
              case "$line" in WHYGRAPH_VERSION=*) echo "${line#WHYGRAPH_VERSION=}"; break ;; esac
          done
}

# Delegates shim generation to the image's own `whygraph install` (the single
# source of truth), then verifies the pipe was NOT empty before executing it.
install_shims() {
    image="$1"
    tmp=$(mktemp) || die "cannot create a temporary file"
    trap 'rm -f "$tmp"' EXIT INT TERM
    docker run --rm "$image" whygraph install > "$tmp" \
        || die "'$image' could not emit the installer."
    [ -s "$tmp" ] || die \
        "'$image' emitted an empty installer — it may predate the 'whygraph
install' command (added in 1.0.0). Install natively instead:
  uv tool install whygraph==$VERSION"
    sh "$tmp"
}

path_advice() {
    bin_dir="${WHYGRAPH_BIN_DIR:-$HOME/.local/bin}"
    # The generated installer already warns when bin_dir is off the interactive
    # PATH. This adds the git-hook case, which only the host side can see.
    case ":${PATH:-}:" in *":$bin_dir:"*) ;; *) return 0 ;; esac
    info ""
    info "note: git hooks launched by GUI clients (Sourcetree, Tower, JetBrains,"
    info "      VS Code) often do not inherit $bin_dir, so WhyGraph's auto-rescan"
    info "      hooks will silently skip. If you use one, symlink the shim into a"
    info "      system path:  sudo ln -sf \"$bin_dir/whygraph\" /usr/local/bin/whygraph"
}

main() {
    VERSION="${WHYGRAPH_VERSION:-${1:-$DEFAULT_VERSION}}"
    image="$IMAGE_REPO:$VERSION"

    require_docker
    info "Pulling $image …"
    docker pull "$image" >&2 || die \
        "could not pull $image. Check the version exists: $RELEASES_URL"
    # NB: resolved_version exits 0 even when it finds nothing, so default on
    # the *empty string*, not on exit status.
    resolved=$(resolved_version "$image")
    info "Installing WhyGraph ${resolved:-$VERSION}"
    install_shims "$image"
    path_advice
    info ""
    info "done. Try:  cd <your-repo> && whygraph init && whygraph scan"
}

main "$@"
