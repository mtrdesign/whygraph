"""Build ``src/whygraph/portal/data/common_passwords.txt``.

Keeps only the entries of at least 15 characters (shorter ones are refused by
the length rule anyway), lower-cased, unique and sorted. Run it from the repo
root::

    uv run python scripts/build_password_blocklist.py [SOURCE_FILE]

Without ``SOURCE_FILE`` the list is downloaded from :data:`SOURCE_URL`.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

SOURCE_URL = (
    "https://raw.githubusercontent.com/danielmiessler/SecLists/master/"
    "Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt"
)
MIN_LENGTH = 15
OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "src/whygraph/portal/data/common_passwords.txt"
)
HEADER = f"""\
# Common passwords of {MIN_LENGTH}+ characters, lower-case, one per line.
# Source: {SOURCE_URL}
# SecLists, MIT License, Copyright (c) 2018 Daniel Miessler
# Built by scripts/build_password_blocklist.py - do not edit by hand.
"""


def build(text: str) -> str:
    """Return the blocklist file content for a raw password list.

    Parameters
    ----------
    text : str
        One password per line.

    Returns
    -------
    str
        The header plus the sorted, unique, lower-cased entries of at least
        :data:`MIN_LENGTH` characters (a leading ``#`` entry is dropped so
        it cannot read back as a comment).
    """
    entries = {line.strip().lower() for line in text.splitlines()}
    kept = sorted(e for e in entries if len(e) >= MIN_LENGTH and e[0] != "#")
    return HEADER + "\n".join(kept) + "\n"


def main(argv: list[str]) -> int:
    """Read the source (a path argument or the download) and write the file."""
    if argv:
        text = Path(argv[0]).read_text(encoding="utf-8", errors="replace")
    else:
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", errors="replace")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build(text), encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
