#!/bin/sh
# End-to-end release check (`make check`'s last step, or `sh scripts/smoke.sh IMAGE`):
# install the shim from IMAGE, start the portal through it, and drive a real
# project through setup -> add -> init -> first scan -> MCP -> hook -> catch-up.
#
# Isolated from the user's own portal: a scratch HOME / data / bin dir, the
# containers `whygraph-portal-smoke` and `whygraph-portal-smoke-postgres`, port
# 8797. Exits non-zero naming the first failed step; always removes its
# containers and scratch dir.
set -eu

IMAGE=${1:?usage: smoke.sh IMAGE}
PORT=${SMOKE_PORT:-8797}
P="http://127.0.0.1:$PORT"
H='X-WhyGraph-Client: 1'
S=$(mktemp -d "${TMPDIR:-/tmp}/whygraph-smoke.XXXXXX")
S=$(cd -- "$S" && pwd -P)

export DOCKER_CONFIG="${DOCKER_CONFIG:-$HOME/.docker}"
export HOME="$S/home" WHYGRAPH_BIN_DIR="$S/bin" WHYGRAPH_IMAGE="$IMAGE" WHYGRAPH_PORT="$PORT"
export WHYGRAPH_PORTAL_NAME=whygraph-portal-smoke
unset WHYGRAPH_DEV_SRC WHYGRAPH_DATA 2>/dev/null || true
export PATH="$S/bin:$PATH"
mkdir -p "$HOME" "$S/bin" "$S/share"
# Always this run's own shim, never one found on PATH: a user's installed
# 2.0 shim ignores WHYGRAPH_PORTAL_NAME and would stop their real portal.
wg="$S/bin/whygraph"
whygraph() {
    if [ -x "$wg" ]; then "$wg" "$@"; else echo "smoke: no shim installed" >&2; return 1; fi
}

# Production-mode section: direct docker runs (the shim passes no environment).
PROD_NAME=whygraph-portal-smoke-prod
PROD_PORT=${SMOKE_PROD_PORT:-8798}
PROD_BASE="http://whygraph.localhost:$PROD_PORT"
PROD_ORG="http://acme.whygraph.localhost:$PROD_PORT"
PROD_BRAVO="http://bravo.whygraph.localhost:$PROD_PORT"
# The fake GitHub (tests/github_fake.py) runs in the portal container's network
# namespace, so the portal and the host-side curl both reach it on loopback. It
# serves both apps, and its fixture repos (created in its own container, which
# has git) over dumb HTTP; its control routes deliver webhooks to the portal's
# in-container port with the base host as Host.
FAKE_PORT=${SMOKE_GITHUB_PORT:-18767}
FAKE="http://127.0.0.1:$FAKE_PORT"
FAKE_PY=$(cd -- "$(dirname -- "$0")/.." && pwd -P)/tests/github_fake.py

step=""
passed=0
cleanup() {
    code=$?
    [ ! -x "$wg" ] || "$wg" down >/dev/null 2>&1 || true
    docker rm -f "$PROD_NAME-github" "$PROD_NAME" "$PROD_NAME-postgres" >/dev/null 2>&1 || true
    docker network rm "$PROD_NAME" >/dev/null 2>&1 || true
    rm -rf "$S"
    if [ "$code" -ne 0 ]; then
        echo "SMOKE FAILED at: $step" >&2
    else
        echo "smoke OK ($passed checks) -> $IMAGE"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

check() {  # check "<label>" <command...>: the command must succeed
    step=$1
    shift
    if "$@"; then
        passed=$((passed + 1))
        echo "  ok  $step"
    else
        echo "  FAIL $step" >&2
        [ ! -s "$S/last-body" ] || { echo "  last response: $(head -c 600 "$S/last-body")" >&2; }
        exit 1
    fi
}
api() { m=$1 p=$2; shift 2; curl -sS -X "$m" -H "$H" -H 'Content-Type: application/json' "$P$p" "$@"; }
# HTTP status only; the body goes to $S/last-body for the failure report.
code_of() { m=$1 p=$2; shift 2; curl -sS -o "$S/last-body" -w '%{http_code}' -X "$m" -H "$H" -H 'Content-Type: application/json' "$P$p" "$@"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
runs() { api GET /api/projects/demo/scans | json '" ".join(r["trigger"] + ":" + r["status"] for r in d["runs"])'; }
wait_portal() {
    i=0
    while [ $i -lt 60 ]; do
        curl -fsS -o /dev/null -H "$H" "$P/api/portal/state" 2>/dev/null && return 0
        i=$((i + 1)); sleep 1
    done
    return 1
}
wait_run() {  # wait_run <trigger>: until a run with that trigger finishes; prints its status
    i=0
    while [ $i -lt 120 ]; do
        st=$(api GET /api/projects/demo/scans | json "next((r['status'] for r in d['runs'] if r['trigger']=='$1' and r['status'] not in ('queued','running')), '')")
        if [ -n "$st" ]; then echo "$st"; return 0; fi
        i=$((i + 1)); sleep 1
    done
    echo timeout
}
commit() { git -C "$R" -c user.name=t -c user.email=t@x -c commit.gpgsign=false commit -q "$@"; }

echo "== install"
step="install from $IMAGE"
installer=$(docker run --rm "$IMAGE" whygraph install) || exit 1
[ -n "$installer" ] || { echo "empty installer output from $IMAGE" >&2; exit 1; }
printf '%s\n' "$installer" | sh >/dev/null
check "install writes the whygraph shim" test -x "$S/bin/whygraph"
check "install writes the whygraph-mcp stub" test -x "$S/bin/whygraph-mcp"
mcp_rc=0; "$S/bin/whygraph-mcp" >/dev/null 2>&1 || mcp_rc=$?
check "whygraph-mcp is a removal stub (exit 2)" test "$mcp_rc" -eq 2
want_version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$(dirname -- "$0")/../pyproject.toml" | head -1)
check "image reports the pyproject version ($want_version)" \
    sh -c "docker run --rm '$IMAGE' whygraph version | grep -q -- '$want_version'"

echo "== demo repo"
R="$S/share/demo"
mkdir -p "$R/src"
git -C "$R" init -q -b main
printf 'def add(a, b):\n    return a + b\n' > "$R/src/calc.py"
git -C "$R" add -A && commit -m "feat: add calc"
printf '\n\ndef mul(a, b):\n    return a * b\n' >> "$R/src/calc.py"
commit -am "feat: add mul"

echo "== portal"
whygraph up --add-folder "$S/share" >/dev/null
check "portal answers" wait_portal
check "setup" test "$(code_of POST /api/portal/setup -d '{"display_name":"Smoke"}')" = 201
check "check-path refuses a path outside the shared folders" \
    test "$(api POST /api/portal/check-path -d '{"path":"/etc"}' | json 'd["shared"]')" = False
add_body=$(printf '{"source":"local","path":"%s"}' "$R")
check "add a local project" test "$(code_of POST /api/projects -d "$add_body")" = 201
check "data routes answer 409 before init" test "$(code_of GET /api/projects/demo/tree)" = 409
check "initialize (claude + cursor)" \
    test "$(code_of POST /api/projects/demo/init -d '{"agents":["claude","cursor"]}')" = 200
hooks_dir=$(git -C "$R" rev-parse --git-path hooks)
for h in post-commit post-merge post-rewrite post-checkout; do
    check "hook $h installed" grep -q 'whygraph managed' "$R/$hooks_dir/$h"
done
check "portal.env marker" grep -q "^port=$PORT" "$R/.whygraph/portal.env"
check "portal.json marker" test -f "$R/.whygraph/portal.json"
check "claude MCP entry is HTTP" grep -q "/mcp/demo" "$R/.mcp.json"

echo "== first scan"
check "request a scan" test "$(code_of POST /api/projects/demo/scans -d '{"trigger":"manual"}')" = 202
check "first scan is 'initial' and ends ok" test "$(wait_run initial)" = ok

echo "== MCP"
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
check "MCP initialize" sh -c "curl -sS -X POST '$P/mcp/demo' -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' -d '$init' | grep -q serverInfo"
check "MCP tools/list has whygraph_evidence_for" sh -c "curl -sS -X POST '$P/mcp/demo' \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}' | grep -q whygraph_evidence_for"

echo "== host CLI in a managed repo"
scan_rc=0; (cd "$R" && whygraph scan >/dev/null 2>&1) || scan_rc=$?
check "host 'whygraph scan' refuses (exit 2)" test "$scan_rc" -eq 2

echo "== hooks"
printf '\n\ndef sub(a, b):\n    return a - b\n' >> "$R/src/calc.py"
commit -am "feat: add sub"
check "a commit triggers a hook scan that ends ok" test "$(wait_run hook)" = ok
whygraph down >/dev/null
commit --allow-empty -m "chore: empty"
sleep 3
check "portal down: the hook logs one line" test "$(wc -l < "$R/.whygraph/logs/hooks.log" | tr -d ' ')" = 1

echo "== catch-up"
whygraph up >/dev/null
check "portal answers again" wait_portal
# down + up removed and recreated both containers: the project must come back
# from the portal database's files in the data dir, not from memory.
listed() { api GET /api/projects | json '" ".join(p["slug"] for p in d["projects"])' | tr ' ' '\n' | grep -qx demo; }
check "the project added earlier is still listed after down + up (portal database persisted)" listed
hook_ok() { runs | tr ' ' '\n' | grep -c '^hook:ok' || true; }
i=0
while [ $i -lt 60 ] && [ "$(hook_ok)" -lt 2 ]; do i=$((i + 1)); sleep 1; done
check "restart catches up the missed commit (a second hook scan)" test "$(hook_ok)" -ge 2

echo "== production mode"
# The shim has no production verbs, so run the image directly: a network, a
# Postgres of the pinned image, and the portal with the three production
# variables. The base URL carries the *published* port.
step="start the production-mode portal"
pg_image=$(docker run --rm "$IMAGE" python -c 'from whygraph.cli.commands.install import POSTGRES_IMAGE; print(POSTGRES_IMAGE)')
[ -n "$pg_image" ] || { echo "cannot read POSTGRES_IMAGE from $IMAGE" >&2; exit 1; }
docker network create "$PROD_NAME" >/dev/null
docker run -d --name "$PROD_NAME-postgres" --network "$PROD_NAME" \
    --network-alias postgres --tmpfs /var/lib/postgresql \
    --health-cmd "pg_isready -h 127.0.0.1 -U whygraph -d whygraph" \
    --health-interval 2s --health-timeout 3s --health-retries 30 --health-start-period 5s \
    -e POSTGRES_USER=whygraph -e POSTGRES_DB=whygraph -e POSTGRES_PASSWORD=smoke-prod \
    "$pg_image" >/dev/null
pg_ready() {
    i=0
    while [ $i -lt 60 ]; do
        [ "$(docker inspect -f '{{.State.Health.Status}}' "$PROD_NAME-postgres" 2>/dev/null)" = healthy ] && return 0
        i=$((i + 1)); sleep 1
    done
    return 1
}
check "production postgres is healthy" pg_ready
printf 'smoke-client-secret\n' > "$S/github-secret"
printf 'smoke-app-client-secret\n' > "$S/github-app-secret"
printf 'smoke-webhook-secret-0123456789abcdef\n' > "$S/github-webhook-secret"
# The GitHub App's key, generated inside the image (no openssl needed on the host).
docker run --rm "$IMAGE" python -c 'from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric import rsa
k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
print(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode(), end="")' \
    > "$S/github-app-key.pem"
check "the GitHub App key is generated in the image" grep -q 'BEGIN PRIVATE KEY' "$S/github-app-key.pem"
chmod 644 "$S/github-secret" "$S/github-app-secret" "$S/github-webhook-secret" "$S/github-app-key.pem"
secrets_ro() {  # the four secret files, read-only, at the same paths in both containers
    for f in github-secret github-app-secret github-webhook-secret github-app-key.pem; do
        printf -- '-v\n%s/%s:/run/secrets/%s:ro\n' "$S" "$f" "$f"
    done
}
# shellcheck disable=SC2046  # one mount argument per line
docker run -d --name "$PROD_NAME" --network "$PROD_NAME" --init \
    -p "127.0.0.1:$PROD_PORT:8765" -p "127.0.0.1:$FAKE_PORT:$FAKE_PORT" \
    $(secrets_ro) \
    -e WHYGRAPH_MODE=production -e "WHYGRAPH_BASE_URL=$PROD_BASE" \
    -e WHYGRAPH_GITHUB_OAUTH_CLIENT_ID=smoke-client \
    -e WHYGRAPH_GITHUB_OAUTH_CLIENT_SECRET_FILE=/run/secrets/github-secret \
    -e WHYGRAPH_GITHUB_APP_SLUG=whygraph-smoke -e WHYGRAPH_GITHUB_APP_CLIENT_ID=smoke-app \
    -e WHYGRAPH_GITHUB_APP_CLIENT_SECRET_FILE=/run/secrets/github-app-secret \
    -e WHYGRAPH_GITHUB_APP_PRIVATE_KEY_FILE=/run/secrets/github-app-key.pem \
    -e WHYGRAPH_GITHUB_APP_WEBHOOK_SECRET_FILE=/run/secrets/github-webhook-secret \
    -e "WHYGRAPH_GITHUB_URL=$FAKE" -e "WHYGRAPH_GITHUB_API_URL=$FAKE/api/v3" \
    -e "WHYGRAPH_DATABASE_URL=postgresql+psycopg://whygraph:smoke-prod@postgres:5432/whygraph" \
    "$IMAGE" whygraph portal --host 0.0.0.0 --port 8765 >/dev/null
# shellcheck disable=SC2046
docker run -d --name "$PROD_NAME-github" --network "container:$PROD_NAME" \
    -v "$FAKE_PY:/fake.py:ro" $(secrets_ro) \
    "$IMAGE" python /fake.py --host 0.0.0.0 --port "$FAKE_PORT" \
    --client-id smoke-client --client-secret-file /run/secrets/github-secret \
    --redirect-uri "$PROD_BASE/auth/github" \
    --app-client-id smoke-app --app-client-secret-file /run/secrets/github-app-secret \
    --app-callback "$PROD_BASE/auth/github-app" --app-slug whygraph-smoke \
    --app-key-file /run/secrets/github-app-key.pem \
    --webhook-secret-file /run/secrets/github-webhook-secret \
    --webhook-url http://127.0.0.1:8765/github/webhook --webhook-host "whygraph.localhost:$PROD_PORT" \
    --repos /tmp/fake-repos >/dev/null
jar="$S/prod.jar"
# Real URLs (curl >= 7.85 resolves *.localhost) so the cookie jar matches hosts.
pcode() { m=$1 u=$2; shift 2; curl -sS -o "$S/last-body" -w '%{http_code}' -X "$m" -H "$H" -H 'Content-Type: application/json' "$u" "$@"; }
pget() { u=$1; shift; curl -sS -H "$H" "$u" "$@"; }
is2xx() { case $1 in 2??) return 0 ;; *) return 1 ;; esac; }
prod_up() {
    i=0
    while [ $i -lt 60 ]; do
        curl -fsS -o /dev/null -H "$H" "$PROD_BASE/api/portal/state" 2>/dev/null && return 0
        i=$((i + 1)); sleep 1
    done
    return 1
}
check "production portal answers on the base host" prod_up
check "state: production, base host, bootstrap required" \
    test "$(pget "$PROD_BASE/api/portal/state" | json 'd["mode"] + " " + d["host_kind"] + " " + str(d["bootstrap_required"])')" = "production base True"
secret=$(docker logs "$PROD_NAME" 2>&1 | sed -n 's/.*Bootstrap secret: \([A-Za-z0-9_-]\{24\}\).*/\1/p' | head -n 1)
check "the log prints a bootstrap secret" test -n "$secret"
boot=$(printf '{"secret":"%s","email":"ada@example.com","display_name":"Ada","password":"correct horse battery staple 42"}' "$secret")
check "bootstrap claims the instance" is2xx "$(pcode POST "$PROD_BASE/api/auth/bootstrap" -c "$jar" -d "$boot")"
check "bootstrap is inert once claimed" test "$(pcode POST "$PROD_BASE/api/auth/bootstrap" -d "$boot" | cut -c1)" = 4
check "create org acme" is2xx "$(pcode POST "$PROD_BASE/api/orgs" -b "$jar" -c "$jar" -d '{"slug":"acme","name":"Acme"}')"
check "state on the org host names acme" \
    test "$(pget "$PROD_ORG/api/portal/state" -b "$jar" | json 'd["host_kind"] + " " + str(d["org"]["slug"])')" = "org acme"
check "/mcp does not exist in production" test "$(pcode GET "$PROD_ORG/mcp/x" -b "$jar")" = 404
check "no cookie: the org API answers 401" test "$(pcode GET "$PROD_ORG/api/projects")" = 401

echo "== GitHub sign-in"
fake_up() {
    i=0
    while [ $i -lt 30 ]; do
        curl -sS -o /dev/null "$FAKE/login/oauth/authorize" 2>/dev/null && return 0
        i=$((i + 1)); sleep 1
    done
    return 1
}
check "the fake GitHub answers" fake_up
bjar="$S/ben.jar"
start_body=$(curl -sS -X POST -H "$H" -H 'Content-Type: application/json' -c "$bjar" -d '{}' "$PROD_BASE/api/auth/github/start")
printf '%s' "$start_body" > "$S/last-body"
authorize_url=$(printf '%s' "$start_body" | json 'd["authorize_url"]')
check "start returns an authorize URL on the fake" sh -c "case '$authorize_url' in $FAKE/login/oauth/authorize?*) exit 0 ;; *) exit 1 ;; esac"
location=$(curl -sS -o /dev/null -D - "$authorize_url&login=ben" | tr -d '\r' | sed -n 's/^[Ll]ocation: //p')
cb_body=$(printf '%s' "$location" | python3 -c "import json,sys; from urllib.parse import urlsplit, parse_qs; q=parse_qs(urlsplit(sys.stdin.read().strip()).query); print(json.dumps({'code': q['code'][0], 'state': q['state'][0]}))")
check "the callback signs Ben in" is2xx "$(pcode POST "$PROD_BASE/api/auth/github/callback" -b "$bjar" -c "$bjar" -d "$cb_body")"
check "Ben's session names his GitHub login" \
    test "$(pget "$PROD_BASE/api/account" -b "$bjar" | json 'd["github_login"]')" = ben
check "password registration is gone (404 even signed in)" test "$(pcode POST "$PROD_BASE/api/auth/register" -b "$bjar" -d '{"email":"x@example.com","display_name":"X","password":"correct horse battery staple 42"}')" = 404
check "acme lists exactly one owner" \
    test "$(pget "$PROD_ORG/api/org/members" -b "$jar" | json '" ".join(m["role"] for m in d)')" = owner

echo "== projects from GitHub"
check "Ben creates org bravo" is2xx "$(pcode POST "$PROD_BASE/api/orgs" -b "$bjar" -c "$bjar" -d '{"slug":"bravo","name":"Bravo"}')"
auth_body=$(curl -sS -X POST -H "$H" -H 'Content-Type: application/json' -b "$bjar" -c "$bjar" -d '{}' "$PROD_BRAVO/api/github/app/authorize")
printf '%s' "$auth_body" > "$S/last-body"
app_url=$(printf '%s' "$auth_body" | json 'd["url"]')
check "authorize returns the fake's authorize URL" sh -c "case '$app_url' in $FAKE/login/oauth/authorize?*client_id=smoke-app*) exit 0 ;; *) exit 1 ;; esac"
location=$(curl -sS -o /dev/null -D - "$app_url&login=ben" | tr -d '\r' | sed -n 's/^[Ll]ocation: //p')
app_cb=$(printf '%s' "$location" | python3 -c "import json,sys; from urllib.parse import urlsplit, parse_qs; q=parse_qs(urlsplit(sys.stdin.read().strip()).query); print(json.dumps({k: q[k][0] for k in ('code', 'state', 'iss') if k in q}))")
check "the app callback keeps Ben's user token" is2xx "$(pcode POST "$PROD_BASE/api/github/app/callback" -b "$bjar" -c "$bjar" -d "$app_cb")"
check "the import page lists the fixture installation" \
    test "$(pget "$PROD_BRAVO/api/github/installations" -b "$bjar" | json '" ".join(i["account_login"] + ":" + str(i["id"]) for i in d["installations"])')" = "ben:100"
check "the listing has ben/demo" sh -c "curl -sS -H '$H' -b '$bjar' '$PROD_BRAVO/api/github/installations/100/repos' | grep -q '\"ben/demo\"'"
check "import ben/demo" test "$(pcode POST "$PROD_BRAVO/api/projects" -b "$bjar" -d '{"source":"github","installation_id":100,"repo_id":700001}')" = 201
check "the imported project is initialized" \
    test "$(pget "$PROD_BRAVO/api/projects" -b "$bjar" | json '" ".join(p["slug"] + ":" + str(p["initialized"]) for p in d["projects"])')" = "demo:True"
prod_runs() { pget "$PROD_BRAVO/api/projects/demo/scans" -b "$bjar"; }
prod_wait_run() {  # prod_wait_run <trigger>: until a run with that trigger finishes; prints its status
    i=0
    while [ $i -lt 180 ]; do
        st=$(prod_runs | json "next((r['status'] for r in d['runs'] if r['trigger']=='$1' and r['status'] not in ('queued','running')), '')")
        if [ -n "$st" ]; then echo "$st"; return 0; fi
        i=$((i + 1)); sleep 1
    done
    echo timeout
}
check "request the first scan (fetch + scan)" test "$(pcode POST "$PROD_BRAVO/api/projects/demo/scans" -b "$bjar" -d '{"trigger":"manual"}')" = 202
first_trigger=$(prod_runs | json 'd["runs"][-1]["trigger"]')
first_status=$(prod_wait_run "$first_trigger")
prod_runs > "$S/last-body"
check "the first scan (the real scan child, an installation token) ends ok" test "$first_status" = ok
# The fake's remote is not github.com: the PR crawl is skipped, never failed (plan 0.2 #7).
crawl_ok() {
    prod_runs | json '(lambda cs: all(c.get("status") != "failed" for c in cs) and not any(c.get("name") == "github" and c.get("status") == "ok" for c in cs))((d["runs"][-1]["summary"] or {}).get("crawlers") or [])' | grep -qx True
}
check "the GitHub crawl is skipped, not failed" crawl_ok
push_body=$(curl -sS -X POST -H 'Content-Type: application/json' -d '{"repo":"ben/demo","message":"Pushed in smoke"}' "$FAKE/_fake/push")
printf '%s' "$push_body" > "$S/last-body"
check "a push on the fake is delivered to the webhook (202)" test "$(printf '%s' "$push_body" | json 'd["webhook_status"]')" = 202
check "the push queues a run that fetches and scans it" test "$(prod_wait_run push)" = ok
prod_runs > "$S/last-body"
check "the push run fetched the pushed commit" \
    test "$(prod_runs | json 'next(r for r in d["runs"] if r["trigger"] == "push")["summary"].get("moved")')" = True
