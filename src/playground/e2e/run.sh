#!/bin/sh
# `make e2e`: run the Playwright suite against a throwaway portal.
#
# Starts a throwaway Postgres container (tmpfs, the same flags as the pytest
# fixture in tests/conftest.py) for the portal database, then `whygraph portal`
# on a temp data dir OUTSIDE the checkout (a data dir inside it would make the
# checkout un-addable), with the temp `shared/` folder as its only shared folder
# and the fake scanner (tests/fixtures/e2e_scan.py) standing in for
# `whygraph scan`; runs the suite; tears everything down. Needs Docker.
#
# Environment (all optional):
#   E2E_PORTAL_CMD     how to launch whygraph (default: uv run --no-sync whygraph)
#   E2E_SCAN_PYTHON    interpreter for the fake scanner (default: python3)
#   E2E_PORT           portal port (default: 18765)
#   E2E_PROD_PORT      production-mode portal port (default: 18766)
#   E2E_CHANNEL        browser channel, e.g. `chrome` to use the locally installed
#                      Chrome; unset = Playwright's own Chromium (installed on demand)
#   E2E_KEEP=1         keep the temp dir (portal log, artifacts) after the run
#   WHYGRAPH_TEST_POSTGRES_IMAGE  Postgres image (a mirror); default: the pinned
#                      POSTGRES_IMAGE, read from the checkout
# Any further arguments go to `playwright test` (e.g. --project=light, -g text).
set -eu

here=$(cd "$(dirname "$0")" && pwd -P)
playground=$(dirname "$here")
repo=$(cd "$playground/../.." && pwd -P)

port=${E2E_PORT:-18765}
prod_port=${E2E_PROD_PORT:-18766}
portal_cmd=${E2E_PORTAL_CMD:-uv run --no-sync whygraph}
scan_python=${E2E_SCAN_PYTHON:-python3}

root=$(mktemp -d "${TMPDIR:-/tmp}/whygraph-e2e.XXXXXX")
root=$(cd "$root" && pwd -P)
mkdir "$root/shared" "$root/data" "$root/control"

portal_pid=
prod_pid=
pg_name=
cleanup() {
  status=$?
  if [ -n "$portal_pid" ]; then
    kill "$portal_pid" 2>/dev/null || true
    wait "$portal_pid" 2>/dev/null || true
  fi
  if [ -n "$prod_pid" ]; then
    kill "$prod_pid" 2>/dev/null || true
    wait "$prod_pid" 2>/dev/null || true
  fi
  if [ -n "$pg_name" ]; then
    docker rm -f "$pg_name" >/dev/null 2>&1 || true
  fi
  if [ "$status" -ne 0 ] && [ -f "$root/portal.log" ]; then
    echo "--- portal log (tail) ---" >&2
    tail -n 40 "$root/portal.log" >&2
  fi
  if [ "$status" -ne 0 ] && [ -f "$root/portal-prod.log" ]; then
    echo "--- production portal log (tail) ---" >&2
    tail -n 40 "$root/portal-prod.log" >&2
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

# The portal database: a throwaway server, labelled like the pytest fixture's
# so a later pytest session sweeps it if this run is killed.
pg_image=${WHYGRAPH_TEST_POSTGRES_IMAGE:-$(cd "$repo" && uv run --no-sync python -c \
  'from whygraph.cli.commands.install import POSTGRES_IMAGE; print(POSTGRES_IMAGE)')}
[ -n "$pg_image" ] || { echo "error: cannot read POSTGRES_IMAGE from the checkout" >&2; exit 1; }
pg_name="whygraph-e2e-pg-$$"
docker run -d --rm --name "$pg_name" \
  --label whygraph.test-pg=1 --label "whygraph.test-pg.pid=$$" \
  -p 127.0.0.1::5432 -e POSTGRES_PASSWORD=test --tmpfs /var/lib/postgresql \
  "$pg_image" -c fsync=off -c synchronous_commit=off -c full_page_writes=off >/dev/null
# Ready = the real server answers over TCP (the image's first-run temporary
# server listens on the socket only), then a real query succeeds.
i=0
until docker exec "$pg_name" pg_isready -q -h 127.0.0.1 -U postgres 2>/dev/null \
    && docker exec "$pg_name" psql -h 127.0.0.1 -U postgres -tAc 'select 1' >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "error: the throwaway Postgres ($pg_name) did not become ready" >&2
    docker logs --tail 20 "$pg_name" >&2 || true
    exit 1
  fi
  sleep 0.5
done
pg_port=$(docker port "$pg_name" 5432/tcp | head -n 1)
pg_port=${pg_port##*:}
database_url="postgresql+psycopg://postgres:test@127.0.0.1:$pg_port/postgres"
# The production-mode portal gets its own database on the same server (the
# advisory lock is per database, so two portals cannot share one).
docker exec "$pg_name" psql -h 127.0.0.1 -U postgres -c 'CREATE DATABASE prod' >/dev/null
prod_database_url="postgresql+psycopg://postgres:test@127.0.0.1:$pg_port/prod"
mkdir -p "$root/prod-data"

# Provider keys from the developer's shell must not leak into the run.
(
  cd "$repo"
  unset ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY OPENROUTER_API_KEY GH_TOKEN GITHUB_TOKEN
  unset WHYGRAPH_MODE WHYGRAPH_DEV_ORIGINS WHYGRAPH_CONFIG_JSON WHYGRAPH_DATABASE_PASSWORD_FILE
  export WHYGRAPH_DATABASE_URL="$database_url"
  export WHYGRAPH_SHARED_FOLDERS="$root/shared"
  export WHYGRAPH_SCAN_CMD="$scan_python $repo/tests/fixtures/e2e_scan.py --control $root/control"
  # shellcheck disable=SC2086  # the command is deliberately word-split
  exec $portal_cmd portal --data "$root/data" --port "$port"
) >"$root/portal.log" 2>&1 &
portal_pid=$!

# The production-mode portal: no shared folders, no fake scanner, only the
# three production variables. Its base URL carries the port it listens on.
(
  cd "$repo"
  unset ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY OPENROUTER_API_KEY GH_TOKEN GITHUB_TOKEN
  unset WHYGRAPH_SHARED_FOLDERS WHYGRAPH_DEV_ORIGINS WHYGRAPH_CONFIG_JSON WHYGRAPH_DATABASE_PASSWORD_FILE WHYGRAPH_SCAN_CMD
  export WHYGRAPH_MODE=production
  export WHYGRAPH_BASE_URL="http://whygraph.localhost:$prod_port"
  export WHYGRAPH_DATABASE_URL="$prod_database_url"
  # shellcheck disable=SC2086  # the command is deliberately word-split
  exec $portal_cmd portal --data "$root/prod-data" --port "$prod_port"
) >"$root/portal-prod.log" 2>&1 &
prod_pid=$!

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

i=0
until curl --noproxy '*' -fsS -H "Host: whygraph.localhost:$prod_port" -H 'X-WhyGraph-Client: 1' \
    "http://127.0.0.1:$prod_port/api/portal/state" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ] || ! kill -0 "$prod_pid" 2>/dev/null; then
    echo "error: the production-mode portal did not come up on port $prod_port" >&2
    exit 1
  fi
  sleep 0.5
done

cd "$playground"
export WHYGRAPH_E2E_PROD_URL="http://whygraph.localhost:$prod_port"
export WHYGRAPH_E2E_PROD_LOG="$root/portal-prod.log"
export WHYGRAPH_E2E_ROOT="$root"
export WHYGRAPH_E2E_URL="$url"
export NO_PROXY="127.0.0.1,localhost,whygraph.localhost,.whygraph.localhost${NO_PROXY:+,$NO_PROXY}"
npx playwright test -c e2e/playwright.config.ts "$@"
