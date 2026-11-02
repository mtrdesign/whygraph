"""Checks shared by the project-transfer import helpers (plan 42 section 5.3).

A transfer bundle is untrusted input. Each domain module that owns a table
(notes, chat, rationale cards, descriptions) writes its own imported rows,
and they all refuse the same things the same way:

- a **format violation** (a wrong type, an enum value the target does not
  know, a control character in a single-line field) raises
  :class:`ImportFormatError`, which refuses the whole bundle;
- a **size violation** (an over-long field) makes the helper skip that record
  and count it, so one long card never blocks a move (plan 42 D16).

This module imports nothing from ``mcp/``, ``serve/`` or ``portal/``: the
domain helpers import it, and the portal's import orchestrator maps
:class:`ImportFormatError` to ``400 bundle_invalid`` naming the member and the
line it was processing.
"""

from __future__ import annotations

import re

_REPLACEMENT = "�"

_MULTILINE_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
"""What a multi-line text replaces: :func:`is_control` minus tab, LF and CR,
plus a lone surrogate (U+D800-DFFF), which no database or UTF-8 encoder takes."""

SINGLE_LINE_BAD = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029"
    r"\u202a-\u202e\u2066-\u2069\ud800-\udfff]"
)
"""What a single-line field refuses: :func:`is_control`, the line and paragraph
separators (U+2028, U+2029), every bidi control - the marks (U+061C, U+200E,
U+200F), embeddings and overrides (U+202A-202E) and isolates (U+2066-2069) -
and a lone surrogate (U+D800-DFFF). Each is outside :meth:`str.isprintable`,
so :func:`clean_text`'s fast path never lets one through."""


class ImportFormatError(Exception):
    """A format violation in an imported record: the whole bundle is refused.

    The message names a field and a rule, never the offending value.

    Parameters
    ----------
    field : str
        The record field (or bundle-level rule) that failed.
    reason : str
        Why it failed, without quoting the value.
    index : int, optional
        The 0-based position of the failing record in the sequence the helper
        was given, when it knows it. The orchestrator turns it into a line
        number of the member it passed in.

    Attributes
    ----------
    field : str
        As given.
    reason : str
        As given.
    index : int or None
        As given; a helper may set it after construction.
    """

    def __init__(self, field: str, reason: str, *, index: int | None = None) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason
        self.index = index


def is_control(ch: str) -> bool:
    """Whether ``ch`` is a C0 / C1 control character or DEL.

    Mirrors :func:`whygraph.mcp.notes._is_control`.

    Parameters
    ----------
    ch : str
        One character.

    Returns
    -------
    bool
        ``True`` for U+0000-001F and U+007F-009F.
    """
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def clean_text(
    value: object, *, multiline: bool, max_chars: int, field: str = "text"
) -> str | None:
    """Check an imported text field; ``None`` when it is too long.

    Parameters
    ----------
    value : object
        The field's value; anything but a ``str`` is a format violation.
    multiline : bool
        ``False`` for a single-line field (a name, a model, an id): any
        character of :data:`SINGLE_LINE_BAD` (a C0, DEL or C1 character, a
        line or paragraph separator, a bidi control or a lone surrogate)
        raises :class:`ImportFormatError`. ``True`` for chat and LLM text:
        tab, LF and CR are kept and every other C0, DEL and C1 character
        (NUL included) and every lone surrogate is replaced with U+FFFD;
        nothing is refused.
    max_chars : int
        The field's cap, in characters.
    field : str, optional
        The field name used in an :class:`ImportFormatError`.

    Returns
    -------
    str or None
        The text (unchanged, or with control characters replaced), or
        ``None`` when it is longer than ``max_chars``: the caller skips the
        record and counts it (plan 42 D16).

    Raises
    ------
    ImportFormatError
        ``value`` is not text, or a single-line field holds a character of
        :data:`SINGLE_LINE_BAD`.
    """
    if not isinstance(value, str):
        raise ImportFormatError(field, "must be text")
    if value.isprintable():
        # Fast path: no control (Cc), bidi (Cf), separator (Zl, Zp),
        # surrogate (Cs) or other non-printable character.
        return value if len(value) <= max_chars else None
    # One regex scan (C speed), not a per-character loop: a line break makes
    # every multi-line text miss the fast path, and an import cleans
    # hundreds of thousands of them.
    if multiline:
        value = _MULTILINE_BAD.sub(_REPLACEMENT, value)
    elif SINGLE_LINE_BAD.search(value):
        raise ImportFormatError(field, "must be one line without control characters")
    if len(value) > max_chars:
        return None
    return value
