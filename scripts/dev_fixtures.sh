#!/bin/sh
# Scratch repos every dev mode shares (`make dev-local` / `dev-docker` / `prod`),
# so developing never installs hooks or markers into the real checkout.
# Idempotent and offline:
#
#   <repos>/whygraph   a --no-hardlinks clone of this checkout (real history to dogfood)
#   <repos>/demo       a tiny three-commit Python repo (fast scans)
#
# Delete a repo to get a fresh copy on the next run.
set -eu

repos=$1
checkout=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
mkdir -p "$repos"

if [ ! -d "$repos/whygraph/.git" ]; then
    echo "dev fixtures: cloning this checkout into $repos/whygraph"
    git clone -q --no-hardlinks "$checkout" "$repos/whygraph"
fi

if [ ! -d "$repos/demo/.git" ]; then
    echo "dev fixtures: creating $repos/demo"
    d="$repos/demo"
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
