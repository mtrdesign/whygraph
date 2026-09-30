#!/bin/sh
# `make e2e`: run the Playwright suite against a throwaway portal.
#
# Starts `whygraph portal` on a temp data dir OUTSIDE the checkout (a data dir
# inside it would make the checkout un-addable), with the temp `shared/` folder
# as its only shared folder and the fake scanner (tests/fixtures/e2e_scan.py)
# standing in for `whygraph scan`; runs the suite; tears everything down.
#
# Environment (all optional):
#   E2E_PORTAL_CMD     how to launch whygraph (default: uv run --no-sync whygraph)
#   E2E_SCAN_PYTHON    interpreter for the fake scanner (default: python3)
#   E2E_PORT           portal port (default: 18765)
#   E2E_CHANNEL        browser channel, e.g. `chrome` to use the locally installed
#                      Chrome; unset = Playwright's own Chromium (installed on demand)
#   E2E_KEEP=1         keep the temp dir (portal log, artifacts) after the run
# Any further arguments go to `playwright test` (e.g. --project=light, -g text).
set -eu

here=$(cd "$(dirname "$0")" && pwd -P)
playground=$(dirname "$here")
repo=$(cd "$playground/../.." && pwd -P)

port=${E2E_PORT:-18765}
portal_cmd=${E2E_PORTAL_CMD:-uv run --no-sync whygraph}
scan_python=${E2E_SCAN_PYTHON:-python3}

root=$(mktemp -d "${TMPDIR:-/tmp}/whygraph-e2e.XXXXXX")
root=$(cd "$root" && pwd -P)
mkdir "$root/shared" "$root/data" "$root/control"

portal_pid=
cleanup() {
  status=$?
  if [ -n "$portal_pid" ]; then
    kill "$portal_pid" 2>/dev/null || true
    wait "$portal_pid" 2>/dev/null || true
  fi
  if [ "$status" -ne 0 ] && [ -f "$root/portal.log" ]; then
    echo "--- portal log (tail) ---" >&2
    tail -n 40 "$root/portal.log" >&2
  fi
  if [ "${E2E_KEEP:-}" = 1 ]; then
    echo "kept $root" >&2
  else
    rm -rf "$root"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

if [ -z "${E2E_CHANNEL:-}" ]; then
  (cd "$playground" && npx playwright install chromium)
fi

# Provider keys from the developer's shell must not leak into the run.
(
  cd "$repo"
  unset ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY OPENROUTER_API_KEY GH_TOKEN GITHUB_TOKEN
  unset WHYGRAPH_MODE WHYGRAPH_DEV_ORIGINS WHYGRAPH_CONFIG_JSON
  export WHYGRAPH_SHARED_FOLDERS="$root/shared"
  export WHYGRAPH_SCAN_CMD="$scan_python $repo/tests/fixtures/e2e_scan.py --control $root/control"
  # shellcheck disable=SC2086  # the command is deliberately word-split
  exec $portal_cmd portal --data "$root/data" --port "$port"
) >"$root/portal.log" 2>&1 &
portal_pid=$!

url="http://127.0.0.1:$port"
i=0
until curl -fsS --noproxy '*' -H 'X-WhyGraph-Client: 1' "$url/api/portal/state" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ] || ! kill -0 "$portal_pid" 2>/dev/null; then
    echo "error: the portal did not come up on $url" >&2
    exit 1
  fi
  sleep 0.5
done

cd "$playground"
export WHYGRAPH_E2E_ROOT="$root"
export WHYGRAPH_E2E_URL="$url"
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
npx playwright test -c e2e/playwright.config.ts "$@"
