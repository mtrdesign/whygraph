#!/bin/sh
# Scratch repos every dev mode shares (`make dev-local` / `dev-docker` / `prod`),
# so developing never installs hooks or markers into the real checkout.
# Idempotent and offline:
#
#   <repos>/whygraph   a --no-hardlinks clone of this checkout (real history to dogfood)
#   <repos>/demo       a tiny three-commit Python repo (fast scans)
#   <repos>/why-bench        a twelve-commit repo with constraints in commit bodies
#   <repos>/why-bench-blank  the same final tree as one `init` commit
#
# Delete a repo to get a fresh copy on the next run; one that is no longer a
# working repository (say, its files were deleted but its directories kept) is
# rebuilt the same way.
set -eu

repos=${1:?usage: dev_fixtures.sh <repos dir>}
checkout=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
mkdir -p "$repos"

# True when $1/.git is a repository whose HEAD names a commit. --git-dir keeps
# git from walking up to an enclosing repository when $1/.git is broken.
healthy() { git --git-dir="$1/.git" rev-parse -q --verify 'HEAD^{commit}' >/dev/null 2>&1; }

if ! healthy "$repos/whygraph"; then
    echo "dev fixtures: cloning this checkout into $repos/whygraph"
    rm -rf "$repos/whygraph"
    git clone -q --no-hardlinks "$checkout" "$repos/whygraph"
fi

if ! healthy "$repos/demo"; then
    echo "dev fixtures: creating $repos/demo"
    d="$repos/demo"
    rm -rf "$d"
    mkdir -p "$d/src"
    git -C "$d" init -q -b main
    c() { git -C "$d" -c user.name="WhyGraph Dev" -c user.email=dev@whygraph.invalid -c commit.gpgsign=false commit -q "$@"; }
    printf 'def add(a, b):\n    return a + b\n' > "$d/src/calc.py"
    git -C "$d" add -A && c -m "feat: add calc"
    printf '\n\ndef mul(a, b):\n    return a * b\n' >> "$d/src/calc.py"
    c -am "feat: add mul"
    printf '\n\ndef sub(a, b):\n    return a - b\n' >> "$d/src/calc.py"
    c -am "feat: add sub"
fi

if ! healthy "$repos/why-bench"; then
    echo "dev fixtures: creating $repos/why-bench"
    rm -rf "$repos/why-bench"
    sh "$checkout/scripts/agent_evals/fixtures/why/build.sh" "$repos/why-bench" history
fi

if ! healthy "$repos/why-bench-blank"; then
    echo "dev fixtures: creating $repos/why-bench-blank"
    rm -rf "$repos/why-bench-blank"
    sh "$checkout/scripts/agent_evals/fixtures/why/build.sh" "$repos/why-bench-blank" blank
fi
