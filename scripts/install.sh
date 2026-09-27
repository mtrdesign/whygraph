#!/usr/bin/env sh
# WhyGraph installer. Writes the `whygraph` and `whygraph-mcp` shims onto your
# PATH; each runs the WhyGraph container ephemerally against the current repo.
#
#   curl -fsSL https://raw.githubusercontent.com/mtrdesign/whygraph/v1.1.1/scripts/install.sh | sh
#
# The tag in that URL is the version: DEFAULT_VERSION below matches it, and CI
# fails a release whose tag disagrees. Override with an argument or the env:
#   … | sh -s 1.1.1        … | sh -s latest        WHYGRAPH_VERSION=1.1.1 … | sh
#
# Other env: WHYGRAPH_BIN_DIR (default ~/.local/bin, read by the generated
# installer), WHYGRAPH_IMAGE_REPO (private mirrors).
#
# Every statement lives in a function and `main "$@"` is last, so a truncated
# download defines functions and never runs anything.
set -eu

DEFAULT_VERSION="1.1.1"        # the release that first ships this file; gated by CI.
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

# Pulls $1 with docker's progress streaming to stderr as usual, but also keeps
# docker's error text in $pull_err so a failure can be explained, not guessed.
pull_image() {
    status=0
    pull_err=$( { docker pull "$1" 2>&1 1>&3; } 3>&2 ) || status=$?
    [ -z "$pull_err" ] || printf '%s\n' "$pull_err" >&2
    return "$status"
}

# Dies with a message matched to *why* the pull failed. A 403 used to be
# reported as "check the version exists", which sent users chasing a missing
# release when their registry access was blocked (e.g. Docker Desktop's
# Registry Access Management, which allows Docker Hub but not ghcr.io).
pull_failed() {
    image="$1"
    registry="${IMAGE_REPO%%/*}"
    case "$pull_err" in
        *"manifest unknown"*|*"not found"*) die \
            "could not pull $image, and no local image by that name.
That version does not exist in $registry. Check the releases: $RELEASES_URL" ;;
        *403*|*Forbidden*|*"failed to authorize"*|*denied*|*unauthorized*) die \
            "could not pull $image, and no local image by that name.
Docker was refused access to $registry (see its error above). The version is
probably fine: this is usually a Docker Desktop registry allowlist (Registry
Access Management) or a proxy blocking $registry - or, for a private mirror,
a missing 'docker login $registry'. Workarounds:
  - fetch the image outside Docker, load it, then re-run this installer:
      crane pull $image whygraph.tar && docker load -i whygraph.tar
  - use a mirror Docker may pull from:  … | WHYGRAPH_IMAGE_REPO=<mirror>/whygraph sh
  - install without Docker:
      uv tool install \"git+https://github.com/mtrdesign/whygraph.git@v$VERSION\"" ;;
        *) die \
            "could not pull $image, and no local image by that name (docker's
error is above). Check the version exists: $RELEASES_URL" ;;
    esac
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
    # A pull failure is only fatal if the image isn't already local — that is
    # what lets a locally built or `docker load`ed image (air-gapped hosts, and
    # this repo's own integration checks) install without a registry. A bad tag
    # has neither, so it still dies loudly.
    pull_image "$image" || docker image inspect "$image" >/dev/null 2>&1 \
        || pull_failed "$image"
    # NB: resolved_version exits 0 even when it finds nothing, so default on
    # the *empty string*, not on exit status.
    resolved=$(resolved_version "$image")
    info "Installing WhyGraph ${resolved:-$VERSION}"
    # The generated installer prints its own "installed …" lines and the
    # closing "done. Try: …" hint, so nothing is echoed after this but the
    # host-only advice it cannot know about.
    install_shims "$image"
    path_advice
}

main "$@"
