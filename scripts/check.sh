#!/bin/sh
# `make check` / `make check-fast`: everything CI runs, the stages in parallel.
#
#   sh scripts/check.sh IMAGE          lint, pytest, frontend + e2e, image + smoke
#   sh scripts/check.sh --fast         the same without the image and the smoke test
#
# The independent chains run at once, each into its own log under a temp dir:
#   lint        ruff check + ruff format --check
#   pytest      the suite, on every core (xdist)
#   frontend    npm ci (only when the lockfile changed), typecheck, Vitest,
#               build, then the e2e suite (it needs the build)
#   image       the release image, then scripts/smoke.sh on it (not --fast)
# Prints one line per stage as it ends, then the log of every failed one, and
# exits non-zero when any failed. Logs are kept on failure (CHECK_KEEP=1: always).
set -u

fast=0
image=
case "${1:-}" in
  --fast) fast=1 ;;
  "") echo "usage: check.sh IMAGE | --fast" >&2; exit 2 ;;
  *) image=$1 ;;
esac

uv_run=${UV_RUN:-uv run}
logs=$(mktemp -d "${TMPDIR:-/tmp}/whygraph-check.XXXXXX")
start=$(date +%s)

lint() {
  $uv_run ruff check src/ tests/ scripts/ && $uv_run ruff format --check src/ tests/ scripts/
}

pytest_stage() {
  $uv_run pytest -q
}

frontend() {
  # npm records what it installed in node_modules/.package-lock.json.
  if ! cmp -s src/playground/package-lock.json src/playground/node_modules/.package-lock.json 2>/dev/null; then
    npm --prefix src/playground ci || return 1
  fi
  npm --prefix src/playground run typecheck \
    && npm --prefix src/playground test \
    && npm --prefix src/playground run build \
    && sh src/playground/e2e/run.sh
}

image_stage() {
  make -s image IMAGE="$image" && sh scripts/smoke.sh "$image"
}

run() {  # run NAME FUNCTION: in the background, into $logs/NAME.log
  (
    t0=$(date +%s)
    if "$2" >"$logs/$1.log" 2>&1; then status=ok; else status=FAILED; fi
    echo "$status" >"$logs/$1.status"
    printf '%-9s %-7s %4ss\n' "$1" "$status" "$(( $(date +%s) - t0 ))"
  ) &
}

stages="lint pytest frontend"
run lint lint
run pytest pytest_stage
run frontend frontend
if [ "$fast" = 0 ]; then
  stages="$stages image"
  run image image_stage
fi
wait

failed=
for s in $stages; do
  [ "$(cat "$logs/$s.status" 2>/dev/null)" = ok ] || failed="$failed $s"
done
echo "total     $(( $(date +%s) - start ))s"
if [ -n "$failed" ]; then
  for s in $failed; do
    echo
    echo "===== $s (last 60 lines of $logs/$s.log) ====="
    tail -n 60 "$logs/$s.log"
  done
  echo
  echo "FAILED:$failed (logs in $logs)"
  exit 1
fi
[ "${CHECK_KEEP:-0}" = 1 ] && echo "logs in $logs" || rm -rf "$logs"
echo "all checks passed"
