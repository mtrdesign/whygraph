"""The reference pre-pass: ground a question in what WhyGraph can resolve.

Before a new question reaches the model, the identifiers it names - symbols,
file paths, ``#N`` pull requests and issues, commit SHAs - are looked up in
CodeGraph's index and the project DB, and what resolved is appended to the
user message under a fixed heading as a small JSON list. The model then
knows, before its first round, which names are real, where they live, and
whether a rationale card is already cached, so its first tool call can go
straight at them.

Two rules shape everything here:

* **Never fail a turn.** :func:`ground` catches every exception, logs it and
  returns ``None`` (no block); a wall-clock deadline and the turn's cancel
  check stop the work between references.
* **Never invent.** Only what a lookup returned appears; a name that did not
  resolve is listed as ``"resolved": false``, and nothing is listed when
  nothing resolved. The block carries identifiers and states only - never a
  title, a message or any other free text a forge or repo supplied.

The module is pure over the lookups it is handed (:class:`ReferenceLookups`);
the serve layer supplies them, runs the pre-pass, stores the block on the
user row (``chat_message.grounding``) and re-appends it on every later turn
with :func:`with_references`, so the request prefix stays byte-identical and
the provider's prompt cache keeps holding.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from whygraph.services.codegraph import Symbol

__all__ = [
    "DEADLINE_SEC",
    "MAX_REFERENCES",
    "MAX_SUFFIX_CHARS",
    "MAX_SYMBOLS",
    "MAX_SYMBOLS_PER_REFERENCE",
    "REFERENCES_HEADING",
    "ReferenceLookups",
    "extract_references",
    "ground",
    "references_suffix",
    "resolve_references",
    "with_references",
]

_log = logging.getLogger(__name__)

REFERENCES_HEADING = "## References in this question (data, resolved by WhyGraph)"
"""The fixed heading the block sits under; ``system.md``'s data rule names it."""

MAX_REFERENCES = 10
"""Most references extracted from one question."""

MAX_SYMBOLS_PER_REFERENCE = 3
"""Most symbols one name resolves to (a name defined in several places)."""

MAX_SYMBOLS = 5
"""Most symbols in one block, across every reference."""

MAX_SUFFIX_CHARS = 2_000
"""Ceiling on what :func:`with_references` appends (heading and list)."""

DEADLINE_SEC = 1.5
"""Wall-clock budget of one pre-pass."""

_MAX_TOKEN_CHARS = 200
"""A backticked span longer than this is prose or code, not a reference."""

# Extensions CodeGraph indexes; a bare path must end in one to be taken.
_INDEXED_EXTENSIONS = (
    "astro c cc cjs cpp cs cts cxx dart go h hh hpp hxx java js jsx kt kts "
    "liquid lua mjs mts php py pyi rb rs scala svelte swift ts tsx vue yaml yml"
).split()
# Extensions of files people name bare ("pyproject.toml"): not symbols.
_FILE_EXTENSIONS = frozenset(
    _INDEXED_EXTENSIONS
    + "cfg css html ini json lock md mdx rst sh sql toml txt xml".split()
)

_FENCE = re.compile(r"```")
_URL = re.compile(r"\bhttps?://\S+")
_BACKTICKED = re.compile(r"`([^`\n]+)`")
_CALL_SUFFIX = re.compile(r"\(.*\)$")
_PATH = re.compile(
    r"(?<![\w./@-])(?:\./)?((?:[\w.-]+/)+[\w.-]+\.(?:"
    + "|".join(_INDEXED_EXTENSIONS)
    + r"))(?![\w/])"
)
# A "note #12" is a team note, not pull request or issue 12.
_NUMBER = re.compile(r"(?<![\d_&#/])(?<![nN][oO][tT][eE] )#(\d{1,9})\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
# A dotted or ``::`` name, a call ``name()``, snake_case (two or more
# segments, any case) or camel / Pascal case (a case change inside).
_IDENTIFIER = re.compile(
    r"(?<![\w.:])("
    r"[A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)+"
    r"|[A-Za-z_]\w*(?=\(\))"
    r"|_*[A-Za-z][A-Za-z0-9]*(?:_+[A-Za-z0-9]+)+_*"
    r"|[A-Za-z][A-Za-z0-9]*(?:[a-z0-9][A-Z]|[A-Z]{2}[a-z])[A-Za-z0-9]*"
    r")(?![\w])"
)
_SHA_TOKEN = re.compile(r"[0-9a-fA-F]{7,40}")
_NUMBER_TOKEN = re.compile(r"#(\d{1,9})")


class ReferenceLookups(Protocol):
    """The reads the pre-pass needs, supplied by the caller.

    Every lookup is exact and indexed: no substring search, no re-sync, no
    file read. A lookup may raise; :func:`ground` turns that into no block.

    Attributes
    ----------
    indexed : bool
        Whether CodeGraph's index can be read. When ``False`` symbol and
        path references are skipped, not reported unresolved, because
        "not found" would be a claim the pre-pass cannot make.
    """

    indexed: bool

    def symbol(self, qualified_name: str) -> Symbol | None:
        """The symbol with exactly this qualified name, or ``None``."""

    def symbols_named(self, name: str, limit: int) -> Sequence[Symbol]:
        """Symbols whose short name is exactly ``name``, best first."""

    def rationale_cached(self, symbol: Symbol) -> bool:
        """Whether a rationale card is cached for exactly this symbol's range."""

    def note_count(self, symbol: Symbol) -> int:
        """How many confirmed team notes apply to this symbol (a count only)."""

    def has_file(self, path: str) -> bool:
        """Whether CodeGraph indexed the repository-relative ``path``."""

    def pull_request_state(self, number: int) -> str | None:
        """The stored state of pull request ``number``, or ``None``."""

    def issue_state(self, number: int) -> str | None:
        """The stored state of issue ``number``, or ``None``."""

    def commit_shas(self, prefix: str, limit: int) -> Sequence[str]:
        """Full SHAs of stored commits starting with lowercase ``prefix``."""


def _mask(text: str, pattern: re.Pattern[str]) -> str:
    """``text`` with each match of ``pattern`` blanked to spaces (offsets kept)."""
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _symbol_shaped(token: str) -> bool:
    """Whether a dotted token reads as a name rather than an abbreviation or a file.

    ``e.g`` and ``U.S`` have no segment of two characters; ``README.md`` and
    ``pyproject.toml`` end in a file extension.
    """
    if "." not in token or "::" in token:
        return True
    segments = token.split(".")
    if segments[-1].lower() in _FILE_EXTENSIONS:
        return False
    return len(token) >= 4 and any(len(s) >= 2 for s in segments)


def _is_sha(token: str) -> bool:
    """Whether a bare token reads as a commit SHA: hex with a digit and a letter."""
    return (
        _SHA_TOKEN.fullmatch(token) is not None
        and any(c.isdigit() for c in token)
        and any(c.isalpha() for c in token)
    )


def _backticked(span: str) -> str | None:
    """The reference inside one backticked span, or ``None``.

    A call's arguments are dropped (```run_turn(history)``` names
    ``run_turn``); a span with whitespace is a command or prose.
    """
    token = _CALL_SUFFIX.sub("", span.strip()).removeprefix("./")
    if not token or len(token) > _MAX_TOKEN_CHARS or any(c.isspace() for c in token):
        return None
    return token


def extract_references(text: str) -> tuple[str, ...]:
    """The identifiers a question names, in priority order, deduplicated.

    Backticked spans come first (the user marked them), then, in the order
    they appear, paths with ``/`` and an indexed extension, ``#N``, 7-40
    character hex SHAs (with a digit and a letter), and names: dotted or
    ``::`` qualified names, ``name()`` calls, snake_case and camel /
    Pascal case. URLs are ignored, and a span claimed by one rule is not
    read again by a later one.

    Parameters
    ----------
    text : str
        The user's question.

    Returns
    -------
    tuple of str
        At most :data:`MAX_REFERENCES` tokens. ``#N`` keeps its ``#``.
    """
    found: list[str] = []
    text = _mask(_FENCE.sub("   ", text), _URL)
    for match in _BACKTICKED.finditer(text):
        token = _backticked(match.group(1))
        if token is not None:
            found.append(token)
    text = _mask(text, _BACKTICKED)

    hits: list[tuple[int, str]] = []
    for pattern, take in (
        (_PATH, lambda m: m.group(1)),
        (_NUMBER, lambda m: m.group(0)),
        (_HEX, lambda m: m.group(0) if _is_sha(m.group(0)) else None),
        (_IDENTIFIER, lambda m: m.group(1) if _symbol_shaped(m.group(1)) else None),
    ):
        for match in pattern.finditer(text):
            token = take(match)
            if token is not None:
                hits.append((match.start(), token))
        text = _mask(text, pattern)
    found.extend(token for _, token in sorted(hits, key=lambda hit: hit[0]))
    return tuple(dict.fromkeys(found))[:MAX_REFERENCES]


def _symbol_entries(
    token: str, lookups: ReferenceLookups, room: int
) -> list[dict] | None:
    """Entries for a name: a qualified name exactly, else its definitions by name.

    A qualified token (``.`` or ``::``) is looked up as written and, for
    ``Class.method``, as ``Class::method`` (CodeGraph's form); a short name
    lists its definitions, shortest qualified name first. Import nodes are
    not definitions and never count. ``None`` when nothing matched.
    """
    symbols: list[Symbol] = []
    if "." in token or "::" in token:
        candidates = [token]
        if "::" not in token:
            candidates.append(token.replace(".", "::"))
        for candidate in candidates:
            symbol = lookups.symbol(candidate)
            if symbol is not None and symbol.kind != "import":
                symbols = [symbol]
                break
    if not symbols:
        # Two spare rows, so an import sharing the name cannot crowd out a
        # definition.
        named = lookups.symbols_named(token, MAX_SYMBOLS_PER_REFERENCE + 2)
        symbols = [s for s in named if s.kind != "import"]
    if not symbols:
        return None
    entries = []
    for symbol in symbols[: min(room, MAX_SYMBOLS_PER_REFERENCE)]:
        entry = {
            "text": token,
            "kind": symbol.kind,
            "qualified_name": symbol.qualified_name,
            "file": symbol.file_path,
            "rationale_cached": bool(lookups.rationale_cached(symbol)),
        }
        # A count, never a body: note text stays out of the question.
        if (notes := lookups.note_count(symbol)) > 0:
            entry["notes"] = notes
        entries.append(entry)
    return entries


def _number_entries(token: str, number: int, lookups: ReferenceLookups) -> list[dict]:
    """Entries for ``#N``: the pull request and/or issue with that number."""
    entries = []
    for kind, state in (
        ("pull_request", lookups.pull_request_state(number)),
        ("issue", lookups.issue_state(number)),
    ):
        if state is not None:
            entries.append(
                {"text": token, "kind": kind, "number": number, "state": state}
            )
    return entries


def resolve_references(
    refs: Sequence[str],
    lookups: ReferenceLookups,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """Resolve each reference against ``lookups``, stopping at the deadline.

    A ``#N`` is checked against pull requests and issues (number and state
    only), a SHA by prefix (a unique match gives the full SHA; several give
    ``"ambiguous": true``), a path against CodeGraph's files, anything else
    as a symbol (at most :data:`MAX_SYMBOLS_PER_REFERENCE` per name and
    :data:`MAX_SYMBOLS` in all; a name past that budget is left out). A
    reference that did not resolve is listed as ``"resolved": false``.

    Parameters
    ----------
    refs : Sequence[str]
        From :func:`extract_references`.
    lookups : ReferenceLookups
        The reads; they may raise (the caller, :func:`ground`, catches).
    deadline : float
        A :func:`time.monotonic` instant; checked before each reference.
    cancelled : callable, optional
        Checked before each reference too; ``True`` stops the pass.

    Returns
    -------
    list of dict
        One or more entries per reference, in reference order; what was
        resolved before a stop is kept.
    """
    entries: list[dict] = []
    symbols_left = MAX_SYMBOLS
    for token in refs:
        if time.monotonic() >= deadline:
            _log.info("reference pre-pass hit its deadline after %d", len(entries))
            break
        if cancelled is not None and cancelled():
            break
        found: list[dict] | None
        if number := _NUMBER_TOKEN.fullmatch(token):
            found = _number_entries(token, int(number.group(1)), lookups)
        elif _SHA_TOKEN.fullmatch(token) and any(c.isdigit() for c in token):
            shas = lookups.commit_shas(token.lower(), 2)
            if len(shas) == 1:
                found = [{"text": token, "kind": "commit", "sha": shas[0]}]
            elif shas:
                found = [{"text": token, "kind": "commit", "ambiguous": True}]
            else:
                found = []
        elif not lookups.indexed:
            continue
        elif "/" in token:
            found = (
                [{"text": token, "kind": "file", "file": token}]
                if lookups.has_file(token)
                else []
            )
        else:
            if symbols_left <= 0:
                continue
            found = _symbol_entries(token, lookups, symbols_left) or []
            symbols_left -= len(found)
        entries.extend(found or [{"text": token, "resolved": False}])
    return entries


def _render(entries: Sequence[dict]) -> str:
    """The JSON list, one compact entry per line, in a fixed key order."""
    return (
        "["
        + ",\n ".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            for entry in entries
        )
        + "]"
    )


def references_suffix(resolved: Sequence[dict]) -> str | None:
    """The block's JSON list, or ``None`` when nothing resolved.

    Entries are dropped from the end until :func:`with_references` would
    append at most :data:`MAX_SUFFIX_CHARS`. This string is what the serve
    layer stores in ``chat_message.grounding`` and replays verbatim.

    Parameters
    ----------
    resolved : Sequence[dict]
        From :func:`resolve_references`.

    Returns
    -------
    str or None
        The rendered list, or ``None`` when no entry resolved.
    """
    entries = list(resolved)
    overhead = len(with_references("", ""))
    while entries and len(_render(entries)) + overhead > MAX_SUFFIX_CHARS:
        entries.pop()
    if not any(entry.get("resolved", True) for entry in entries):
        return None
    return _render(entries)


def with_references(content: str, suffix: str) -> str:
    """``content`` with the References block appended - the one concatenation.

    Used for the current turn (:func:`whygraph.chat.harness.run_turn`) and
    for every replay of a past turn (the serve layer's history load), so a
    question carries identical bytes on every request.

    Parameters
    ----------
    content : str
        The user's message as stored.
    suffix : str
        From :func:`references_suffix` (or ``chat_message.grounding``).

    Returns
    -------
    str
        The message the model sees.
    """
    return f"{content}\n\n{REFERENCES_HEADING}\n{suffix}"


def ground(
    text: str,
    lookups: ReferenceLookups,
    *,
    timeout: float = DEADLINE_SEC,
    cancelled: Callable[[], bool] | None = None,
) -> str | None:
    """Run the whole pre-pass on one question; never raise.

    Parameters
    ----------
    text : str
        The user's question.
    lookups : ReferenceLookups
        The reads.
    timeout : float, optional
        Wall-clock budget in seconds (default :data:`DEADLINE_SEC`).
    cancelled : callable, optional
        The turn's cancel check.

    Returns
    -------
    str or None
        The block's JSON list for :func:`with_references`, or ``None`` when
        nothing was named, nothing resolved, or anything failed (logged).
    """
    deadline = time.monotonic() + timeout
    try:
        refs = extract_references(text)
        if not refs:
            return None
        return references_suffix(
            resolve_references(refs, lookups, deadline=deadline, cancelled=cancelled)
        )
    except Exception:  # noqa: BLE001 -- grounding is optional; the turn is not
        _log.warning("the reference pre-pass failed; no block", exc_info=True)
        return None
