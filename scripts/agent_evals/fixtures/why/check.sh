#!/bin/sh
# End-to-end check of the "why" fixtures: both modes build to the same tree,
# the visible tests pass, the hidden tests pass, and each break patch keeps the
# visible tests green while failing its hidden test.
set -eu

here=$(cd -- "$(dirname -- "$0")" && pwd -P)
work=$(mktemp -d "${TMPDIR:-/tmp}/why-check.XXXXXX")
trap 'rm -rf "$work"' EXIT
fail=0
bad() { echo "check.sh: FAIL: $*" >&2; fail=1; }

sh "$here/build.sh" "$work/history" history
sh "$here/build.sh" "$work/blank" blank
a=$(git -C "$work/history" rev-parse 'HEAD^{tree}')
b=$(git -C "$work/blank" rev-parse 'HEAD^{tree}')
[ "$a" = "$b" ] || bad "history and blank trees differ ($a vs $b)"

# run <dir> <module...>: python3 -m unittest in <dir>, quietly.
run() {
    dir=$1; shift
    (cd "$dir" && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest "$@" >/dev/null 2>&1)
}

for s in s1 s2 s3; do
    cp "$here/hidden/test_hidden_$s.py" "$work/history/"
done
run "$work/history" || bad "visible tests fail on the final tree"
for s in s1 s2 s3; do
    run "$work/history" "test_hidden_$s" || bad "hidden $s fails on the final tree"
done

for s in s1 s2 s3; do
    w="$work/break-$s"
    sh "$here/build.sh" "$w" history
    git -C "$w" apply "$here/breaks/$s.patch" || bad "$s.patch does not apply"
    run "$w" || bad "visible tests fail after $s.patch"
    cp "$here/hidden/test_hidden_$s.py" "$w/"
    if run "$w" "test_hidden_$s"; then bad "hidden $s still passes after $s.patch"; fi
done

[ "$fail" = 0 ] && echo "check.sh: ok"
exit "$fail"
