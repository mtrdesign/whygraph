#!/bin/sh
# `make dev-docker` / `make prod`: run a portal from a local image through the
# real `whygraph` shim, in the foreground, isolated from the user's own portal.
#
#   dev_docker.sh dev  IMAGE DEV_DIR PORT   # the checkout mounted over the image, auto-reload + Vite
#   dev_docker.sh prod IMAGE DEV_DIR PORT   # the image exactly as released, no source mount
#
# Isolation: its own container name (whygraph-portal-<mode>), HOME, data dir
# and bin dir under DEV_DIR/<mode>; DOCKER_CONFIG stays on the real ~/.docker so
# the docker CLI keeps its context. Ctrl-C (or the logs ending) runs `down`.
set -eu

mode=$1 image=$2 dev=$3 port=$4
case "$mode" in dev|prod) ;; *) echo "usage: $0 dev|prod IMAGE DEV_DIR PORT" >&2; exit 2 ;; esac
checkout=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
state="$dev/$mode"

export DOCKER_CONFIG="${DOCKER_CONFIG:-$HOME/.docker}"
export HOME="$state/home"
export WHYGRAPH_DATA="$state/data"
export WHYGRAPH_BIN_DIR="$state/bin"
export WHYGRAPH_IMAGE="$image"
export WHYGRAPH_PORTAL_NAME="whygraph-portal-$mode"
if [ "$mode" = dev ]; then
    export WHYGRAPH_DEV_SRC="$checkout"
else
    unset WHYGRAPH_DEV_SRC 2>/dev/null || true
fi
mkdir -p "$HOME" "$WHYGRAPH_BIN_DIR"

# The shim comes out of the image itself, as for a user.
docker run --rm "$image" whygraph install | sh >/dev/null
wg="$WHYGRAPH_BIN_DIR/whygraph"

stopped="" logs_pid=""
stop() {
    [ -z "$stopped" ] || return 0
    stopped=1
    echo
    [ -z "$logs_pid" ] || kill "$logs_pid" 2>/dev/null || true
    "$wg" down || true
}
trap stop EXIT
trap 'exit 130' INT TERM

"$wg" up --port "$port" --add-folder "$dev/repos"
if [ "$mode" = dev ]; then
    echo "open http://localhost:5173 (HMR)   portal + MCP: http://127.0.0.1:$port   Ctrl-C stops"
else
    echo "open http://127.0.0.1:$port   Ctrl-C stops"
fi
# Followed in the background + `wait`, so a signal runs the trap at once
# (sh defers traps until a *foreground* child exits, and `logs -f` never does).
"$wg" logs &
logs_pid=$!
wait "$logs_pid"
