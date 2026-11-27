"""Content anchors for review threads.

A review thread is anchored by the code it was written about: the last few
non-blank new-side lines of its opener's diff hunk, whitespace-normalised.
This module is pure (standard library only) so that ``services/``, ``mcp/``
and ``analyze/`` can all use it without an import cycle. Ownership checks
(whether a matched line is still the PR's own code) are the caller's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

ANCHOR_LINES = 3
ANCHOR_MIN_CHARS = 20


@dataclass(frozen=True)
class Anchor:
    """The code a review thread was written about.

    Attributes
    ----------
    lines : tuple[str, ...]
        Up to ``ANCHOR_LINES`` normalised, non-blank new-side lines; the last
        one is the commented line.
    added : bool
        True when the commented line was added by the PR (``+``), False when it
        was existing context.
    """

    lines: tuple[str, ...]
    added: bool


def normalize_code_line(line: str) -> str:
    """Collapse every whitespace run to one space and strip the ends.

    Parameters
    ----------
    line : str
        One source line, possibly with a trailing ``\\r``.

    Returns
    -------
    str
        The normalised line.
    """
    return " ".join(line.split())


def anchor_from_diff_hunk(hunk: str) -> Anchor | None:
    """Build an anchor from a review comment's diff hunk.

    Parameters
    ----------
    hunk : str
        The unified-diff hunk GitHub attaches to a review comment, which ends
        at the commented line.

    Returns
    -------
    Anchor | None
        The anchor, or ``None`` when fewer than ``ANCHOR_MIN_CHARS``
        non-whitespace characters remain (too generic to identify code).
    """
    kept: list[tuple[str, bool]] = []
    for raw in (hunk or "").split("\n"):
        if raw.startswith("@@") or raw.startswith("\\"):
            continue
        prefix, body = raw[:1], raw[1:]
        if prefix not in (" ", "+"):
            continue
        text = normalize_code_line(body)
        if text:
            kept.append((text, prefix == "+"))
    tail = kept[-ANCHOR_LINES:]
    if not tail:
        return None
    if sum(len("".join(t.split())) for t, _ in tail) < ANCHOR_MIN_CHARS:
        return None
    return Anchor(lines=tuple(t for t, _ in tail), added=tail[-1][1])


def anchor_positions(
    anchor_lines: Sequence[str] | None, lines: Sequence[str] | None
) -> list[int]:
    """Find where an anchor occurs in a file.

    Parameters
    ----------
    anchor_lines : Sequence[str] | None
        Normalised anchor lines.
    lines : Sequence[str] | None
        The file's lines (not yet normalised).

    Returns
    -------
    list[int]
        The original 1-based line number of the last line of each consecutive
        run (blank lines are skipped when matching); ``[]`` for empty inputs.
    """
    if not anchor_lines or not lines:
        return []
    needle = list(anchor_lines)
    n = len(needle)
    numbers: list[int] = []
    texts: list[str] = []
    for i, line in enumerate(lines, start=1):
        text = normalize_code_line(line)
        if text:
            numbers.append(i)
            texts.append(text)
    return [
        numbers[i + n - 1]
        for i in range(len(texts) - n + 1)
        if texts[i : i + n] == needle
    ]


def anchor_status(
    anchor_lines: Sequence[str] | None, lines: Sequence[str] | None
) -> bool | None:
    """Say whether an anchor occurs exactly once in a file.

    Parameters
    ----------
    anchor_lines : Sequence[str] | None
        Normalised anchor lines.
    lines : Sequence[str] | None
        The file's lines.

    Returns
    -------
    bool | None
        True for exactly one position, False for none, None for several or
        when either input is ``None``.
    """
    if anchor_lines is None or lines is None:
        return None
    found = anchor_positions(anchor_lines, lines)
    if len(found) > 1:
        return None
    return len(found) == 1
