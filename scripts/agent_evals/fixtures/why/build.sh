#!/bin/sh
# Build one of the two "why" benchmark fixture repositories.
#
#   build.sh <dir> history   about twelve commits; three carry a constraint in the body
#   build.sh <dir> blank     one commit, "init", with the same final tree
#
# The constraint reasons live in commit bodies only; the last step greps the
# tree for their key words and fails on a hit. Offline and reproducible (fixed
# commit dates).
set -eu

d=${1:?usage: build.sh <dir> <history|blank>}
mode=${2:?usage: build.sh <dir> <history|blank>}
case "$mode" in history | blank) ;; *) echo "build.sh: mode must be history or blank" >&2; exit 2 ;; esac

if [ -e "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ]; then
    echo "build.sh: $d exists and is not empty" >&2
    exit 1
fi
mkdir -p "$d/tests"
git -C "$d" init -q -b main

# Commit everything staged; the date comes from $when (set before each call).
c() {
    git -C "$d" add -A
    GIT_AUTHOR_DATE="$when" GIT_COMMITTER_DATE="$when" \
        git -C "$d" -c user.name="WhyGraph Dev" -c user.email=dev@whygraph.invalid \
        -c commit.gpgsign=false commit -q "$@"
}

# --- file writers (the final ones are shared by both modes) -----------------

w_scaffold() {
    printf '__pycache__/\n' > "$d/.gitignore"
    : > "$d/tests/__init__.py"
    printf '# shop-tools\n\nSmall helpers.\n' > "$d/README.md"
}

w_readme() {
    cat > "$d/README.md" <<'EOT'
# shop-tools

Small helpers: an HTTP fetch, a CSV export and an amount rounder.
Run the tests with `python3 -m unittest`.
EOT
}

w_client_v1() {
    cat > "$d/client.py" <<'EOT'
"""A tiny HTTP client."""

import urllib.request


def _default_send(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read()


def fetch(url, send=None):
    """Return the body of ``url``."""
    send = send or _default_send
    return send(url)
EOT
}

w_client_v2() {
    cat > "$d/client.py" <<'EOT'
"""A tiny HTTP client."""

import urllib.request


def _default_send(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read()


def fetch(url, send=None):
    """Return the body of ``url``, retrying on a timeout."""
    send = send or _default_send
    while True:
        try:
            return send(url)
        except TimeoutError:
            continue
EOT
}

w_client() {
    cat > "$d/client.py" <<'EOT'
"""A tiny HTTP client."""

import urllib.request

MAX_ATTEMPTS = 3


def _default_send(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read()


def fetch(url, send=None):
    """Return the body of ``url``, retrying on a timeout."""
    send = send or _default_send
    attempt = 0
    while True:
        attempt += 1
        try:
            return send(url)
        except TimeoutError:
            if attempt >= MAX_ATTEMPTS:
                raise
EOT
}

w_export_v1() {
    cat > "$d/export.py" <<'EOT'
"""CSV export."""


def write(rows, out):
    """Write one ``id,name`` line per row to the text stream ``out``."""
    for row in rows:
        out.write("%d,%s\n" % (row["id"], row["name"]))
EOT
}

w_export() {
    cat > "$d/export.py" <<'EOT'
"""CSV export."""


def _by_id(row):
    return row["id"]


def write(rows, out, key=None):
    """Write one ``id,name`` line per row to the text stream ``out``."""
    if key is None:
        key = _by_id
    for row in sorted(rows, key=key):
        out.write("%d,%s\n" % (row["id"], row["name"]))
EOT
}

w_price_v1() {
    cat > "$d/price.py" <<'EOT'
"""Amount rounding."""

from decimal import Decimal


def round_amount(value):
    """Round an amount to two decimals."""
    return Decimal(value).quantize(Decimal("0.01"))
EOT
}

w_price() {
    cat > "$d/price.py" <<'EOT'
"""Amount rounding."""

from decimal import ROUND_HALF_EVEN, Decimal

STEP = Decimal("0.01")


def round_amount(value):
    """Round an amount (a ``Decimal`` or a string) to two decimals."""
    return Decimal(value).quantize(STEP, rounding=ROUND_HALF_EVEN)
EOT
}

w_test_client() {
    cat > "$d/tests/test_client.py" <<'EOT'
import unittest

from client import fetch


class FetchTest(unittest.TestCase):
    def test_first_try(self):
        self.assertEqual(fetch("http://x", send=lambda url: b"ok"), b"ok")

    def test_after_one_timeout(self):
        calls = []

        def send(url):
            calls.append(url)
            if len(calls) == 1:
                raise TimeoutError()
            return b"ok"

        self.assertEqual(fetch("http://x", send=send), b"ok")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
EOT
}

w_test_export() {
    cat > "$d/tests/test_export.py" <<'EOT'
import io
import unittest

from export import write

ROWS = [
    {"id": 3, "name": "carol"},
    {"id": 1, "name": "bob"},
    {"id": 2, "name": "alice"},
]


class WriteTest(unittest.TestCase):
    def test_lines(self):
        buf = io.StringIO()
        write(ROWS, buf)
        self.assertEqual(sorted(buf.getvalue().splitlines()), ["1,bob", "2,alice", "3,carol"])

    def test_key_sorts(self):
        buf = io.StringIO()
        write(ROWS, buf, key=lambda row: row["name"])
        self.assertEqual(buf.getvalue().splitlines(), ["2,alice", "1,bob", "3,carol"])


if __name__ == "__main__":
    unittest.main()
EOT
}

w_test_price() {
    cat > "$d/tests/test_price.py" <<'EOT'
import unittest
from decimal import Decimal

from price import round_amount


class RoundAmountTest(unittest.TestCase):
    def test_down(self):
        self.assertEqual(round_amount("1.234"), Decimal("1.23"))

    def test_up(self):
        self.assertEqual(round_amount("1.236"), Decimal("1.24"))

    def test_decimal_input(self):
        self.assertEqual(round_amount(Decimal("5")), Decimal("5.00"))


if __name__ == "__main__":
    unittest.main()
EOT
}

# --- build ------------------------------------------------------------------

if [ "$mode" = blank ]; then
    w_scaffold; w_client; w_export; w_price
    w_test_client; w_test_export; w_test_price; w_readme
    when="2026-01-05T10:00:00+0000"
    c -m "init"
else
    w_scaffold
    when="2026-01-05T10:00:00+0000"; c -m "chore: scaffold the project"

    w_client_v1
    when="2026-01-06T10:00:00+0000"; c -m "feat(client): add fetch"

    w_export_v1
    when="2026-01-07T10:00:00+0000"; c -m "feat(export): write rows as csv"

    w_price_v1
    when="2026-01-08T10:00:00+0000"; c -m "feat(price): add round_amount"

    w_client_v2
    when="2026-01-09T10:00:00+0000"; c -m "feat(client): retry on timeout"

    w_client
    when="2026-01-12T10:00:00+0000"
    c -m "fix(client): cap retries" -m "The total number of attempts stays at 3: the upstream locks a client out after 5 calls in a minute."

    w_test_client
    when="2026-01-13T10:00:00+0000"; c -m "test(client): cover fetch"

    w_export
    when="2026-01-14T10:00:00+0000"
    c -m "refactor(export): order rows" -m "The default order is by id: the receiving side compares files line by line."

    w_test_export
    when="2026-01-15T10:00:00+0000"; c -m "test(export): cover write"

    w_price
    when="2026-01-16T10:00:00+0000"
    c -m "fix(price): round explicitly" -m "Half-even rounding stays: the ledger balances to the cent only with it."

    w_test_price
    when="2026-01-19T10:00:00+0000"; c -m "test(price): cover round_amount"

    w_readme
    when="2026-01-20T10:00:00+0000"; c -m "docs: describe the helpers"
fi

# --- leak check: constraint reasons must not appear anywhere in the tree -----

for word in "lock" "lockout" "5 calls" "per minute" "a minute" \
    "line by line" "receiving" "compares" "diff" \
    "ledger" "balance" "to the cent"; do
    if git -C "$d" grep -n -i -F -e "$word" >/dev/null 2>&1; then
        echo "build.sh: constraint word '$word' leaked into the tree:" >&2
        git -C "$d" grep -n -i -F -e "$word" >&2 || true
        exit 1
    fi
done
