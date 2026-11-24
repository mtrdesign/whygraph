"""Committed plans: the pure builders behind the plan index (plan 61).

A plan is a Markdown file a team commits under ``[plans].dir``. The scan's
Plans phase (:mod:`whygraph.scan.plans_crawler`) reads it from ``HEAD``'s
git objects and stores what this module derives from its (redacted,
``\\n``-normalised) body. Everything here is **pure**: no database, no git,
no config, no LLM, so the crawler and every later read compute the same
values from the same body.

Plan text is untrusted repository content. A harness-written plan also
quotes WhyGraph's own output (card constraints and risks, Prior art);
that **echo text** (:func:`echo_spans`) is never used for linking, for
hints or for the digest.

Public API
----------
* :data:`PLANS_PARSER_VERSION` - bump whenever a rule below changes, so the
  next scan re-derives unchanged blobs.
* :func:`plan_trailers` - the ``WhyGraph-Plan:`` values of a commit or PR body.
* :func:`parse_header` - a plan's title and ``**Objective:**`` line.
* :func:`echo_spans` - the echo text of a body (kinds ``a``-``g``).
* :func:`build_outline` - the stored outline (sections and harness steps).
* :func:`build_digest` - the stored decision digest.
* :func:`build_why_index` - the stored declared changes and decision rows.
* :func:`path_mentions` - the repo paths a plan names outside echo text.
* :func:`entry_at` - the outline entry an offset falls in.

Read side (plan 61 section 4.5; everything below needs the project's WhyGraph
DB and is local-only: on a linked project it answers nothing and makes no
HTTP call):

* :func:`derive_status` - ``shipped`` / ``in_flight`` / ``not_implemented``,
  computed at read time, never stored.
* :func:`plans_for` - the ``plans`` list beside a card (symbol scope) or in
  a file-level answer (file scope); :func:`card_plan_shas` feeds it.
* :func:`plan_record` and :func:`plan_section` - the two resources' bodies.
* :func:`register` - the two MCP resources.
"""

from __future__ import annotations

import bisect
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from whygraph.core import get_config
from whygraph.core.context import current_project
from whygraph.core.utils import LIKE_ESCAPE_CHAR, like_escape
from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange, Plan, PlanLink, PullRequest

from .errors import WhyGraphError

if TYPE_CHECKING:
    from whygraph.analyze.rationale import CommitEvidence
    from whygraph.core.remote import RemoteProject

    from .targets import Target

_log = logging.getLogger(__name__)

PLANS_PARSER_VERSION = 1
"""Version of the outline, digest, why-index and mention rules."""

SECTION_MAX_CHARS = 8000
"""The most characters of one outline entry's own text."""

OUTLINE_MAX_ENTRIES = 300
"""The most outline entries per plan (entry 0 included)."""

OUTLINE_TITLE_MAX = 120
"""The most characters of an outline entry's title."""

DIGEST_MAX_CHARS = 3000
"""The most characters of a plan's digest."""

LINE_CUT_NOTICE = "\n… line cut; continued in the next part"
"""Appended at read time to an entry whose ``cut`` flag is set."""

PLAN_STEM_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
"""What a plan stem (the file name without ``.md``) must fully match."""

MAX_TRAILERS = 5
"""The most ``WhyGraph-Plan:`` values read from one message."""

MAX_WHY_STEPS = 60
"""The most steps in a why index."""

MAX_WHY_DECISIONS = 60
"""The most decision rows in a why index."""

MAX_STEP_FILES = 20
"""The most ``Files:`` paths recorded per step."""

_TITLE_MAX = 200
_SENTENCE_MAX = 4000
_OBJECTIVE_MAX = 300
_DIGEST_FIELD_MAX = 400
_HINT_TITLE_MAX = 80
_DIGEST_QUESTIONS = 3
_DIGEST_RISKS = 3
_FALLBACK_CHARS = 1500

_TRAILER_RE = re.compile(r"(?im)^whygraph-plan:[ \t]*(\S+)[ \t]*$")
_H1_RE = re.compile(r"^#[ \t]+(\S.*)$")
_HEADING_RE = re.compile(r"^(#{2,3})[ \t]+(\S.*)$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_STEP_RE = re.compile(r"^(\d+)\. \*\*(.+?)\*\*")
_FIELD_RE = re.compile(r"^[ \t]*[-*] \*\*([^*\n]+?):\*\*[ \t]?")
_ITEM_RE = re.compile(r"^[-*][ \t]+")
# A basis tag is bounded (200 characters) so a hostile line of unclosed
# "(cited:" openers cannot make a scan quadratic.
_TAG = r"\((?:quoted|cited):[^)\n]{0,200}\)|\(inferred\)|\(note #\d{1,9}\)"
_TAG_RE = re.compile(_TAG)
_TAG_AT_END_RE = re.compile(rf"(?:{_TAG}|\(external\))[ \t]*$")
_PURPOSE_TAIL_RE = re.compile(rf"[ \t]*({_TAG})?[ \t]*(?:-\s|$)")
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._/:-])([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+)(?::\d+(?:-\d+)?)?"
)
_FILES_ENTRY_RE = re.compile(
    r"([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)(?::(\d+)(?:-(\d+))?)?(?=$|[\s(])"
)
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::)[A-Za-z_][A-Za-z0-9_]*)*")
_ROW_ID_RE = re.compile(r"#?[A-Z]{0,2}\d{1,3}[a-z]?")
_SEPARATOR_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_NO_CHANGE_RE = re.compile(r"(?<![A-Za-z])No change needed\b")
_SENTENCE_END_RE = re.compile(r"\.(?=\s|$)|\n\s*\n|\n[ \t]*[-*] ")
_LOG_STEP_RE = re.compile(r"^- \[([ xX])\] Step (\d+):")
_ACCEPTANCE_RE = re.compile(r"^- Acceptance:[ \t]*(.*)$")
_DATE_TAIL_RE = re.compile(r"\s*\((\d{4}-\d{2}-\d{2})[^)]{0,40}\)\s*$")
_QUESTION_ID_RE = re.compile(r"^\*\*(Q\d+[a-z]?)\.?\*\*\.?\s*|^(Q\d+[a-z]?)[.:]\s+")
_LABEL_RE = re.compile(r"\*\*[A-Z][^*\n]*:\*\*")


# --- small helpers -------------------------------------------------------------


def normalise_newlines(text: str) -> str:
    """Return ``text`` with ``\\r\\n`` and ``\\r`` written as ``\\n``.

    Parameters
    ----------
    text : str
        Any text.

    Returns
    -------
    str
        The text with ``\\n`` line endings only.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def line_count(body: str) -> int:
    """Count the lines of ``body`` (a last line without ``\\n`` counts).

    Parameters
    ----------
    body : str
        A ``\\n``-normalised plan body.

    Returns
    -------
    int
        The number of lines; ``0`` for an empty body.
    """
    if not body:
        return 0
    return body.count("\n") + (0 if body.endswith("\n") else 1)


def _clip(text: str, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _heading_title(text: str) -> str:
    """A heading's text without trailing blanks and its optional closing ``#`` run."""
    text = text.rstrip()
    bare = text.rstrip("#")
    if bare != text and (not bare or bare[-1] in " \t"):
        text = bare.rstrip()
    return text


def _escaped(text: str, i: int) -> bool:
    """Whether the character at ``i`` follows an odd run of backslashes."""
    backslashes = 0
    j = i - 1
    while j >= 0 and text[j] == "\\":
        backslashes += 1
        j -= 1
    return backslashes % 2 == 1


def _unescaped_quotes(text: str) -> list[int]:
    """Offsets of every ``"`` in ``text`` that is not written ``\\"``."""
    return [i for i, char in enumerate(text) if char == '"' and not _escaped(text, i)]


def _identifiers(text: str) -> list[str]:
    """Identifier-shaped backtick spans of ``text``, ``()`` dropped, ``.`` as ``::``."""
    names: list[str] = []
    for match in _CODE_SPAN_RE.finditer(text):
        value = match.group(1).strip()
        if value.endswith("()"):
            value = value[:-2]
        if _IDENT_RE.fullmatch(value):
            value = value.replace(".", "::")
            if value not in names:
                names.append(value)
    return names


# --- trailers and header -------------------------------------------------------


def plan_trailers(text: str | None) -> list[str]:
    """The plan stems a commit or PR body links with ``WhyGraph-Plan:`` lines.

    A line matches ``(?im)^whygraph-plan:[ \\t]*(\\S+)[ \\t]*$`` after CRLF
    and CR are normalised. A value that contains ``/`` or ends in ``.md``
    is reduced to its basename stem. Values that do not then fully match
    :data:`PLAN_STEM_RE` are ignored.

    Parameters
    ----------
    text : str or None
        A commit message body or a PR description.

    Returns
    -------
    list of str
        Distinct stems in order of appearance, at most :data:`MAX_TRAILERS`.
    """
    if not text:
        return []
    stems: list[str] = []
    for match in _TRAILER_RE.finditer(normalise_newlines(text)):
        value = match.group(1)
        if "/" in value or value.endswith(".md"):
            value = value.rsplit("/", 1)[-1]
            value = value.removesuffix(".md")
        if PLAN_STEM_RE.fullmatch(value) and value not in stems:
            stems.append(value)
            if len(stems) >= MAX_TRAILERS:
                break
    return stems


def parse_header(body: str) -> tuple[str | None, str | None]:
    """A plan's title and objective.

    The title is the first level-1 heading outside a code fence, with a
    leading ``Plan:`` removed, clipped at 200 characters. The objective is
    the value of the first ``**Objective:**`` line, clipped at 300.

    Parameters
    ----------
    body : str
        The plan body (CRLF and a leading BOM are tolerated).

    Returns
    -------
    tuple of (str or None, str or None)
        ``(title, objective)``; ``None`` for what the plan does not have.
    """
    doc = _Doc(normalise_newlines(body).removeprefix("\ufeff"))
    title: str | None = None
    objective: str | None = None
    for line in doc.lines:
        if line.fenced:
            continue
        if title is None:
            match = _H1_RE.match(line.text)
            if match:
                value = re.sub(r"^Plan:\s*", "", _heading_title(match.group(1)))
                title = _clip(value, _TITLE_MAX) or None
        if objective is None and line.text.lstrip().startswith("**Objective:**"):
            value = line.text.lstrip()[len("**Objective:**") :].strip()
            objective = _clip(value, _OBJECTIVE_MAX) or None
        if title is not None and objective is not None:
            break
    return title, objective


# --- document model --------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    """One line of a body: offsets, text without ``\\n``, fence membership."""

    start: int
    end: int
    text: str
    fenced: bool

    @property
    def text_end(self) -> int:
        """Offset just past the line's text (before its ``\\n``)."""
        return self.start + len(self.text)


@dataclass
class _Field:
    """One ``- **Label:** value`` field of a harness step."""

    label: str
    start: int
    end: int
    line: int


@dataclass
class _Step:
    """One numbered step under ``## Steps``."""

    k: int
    name: str
    first: int
    last: int
    fields: list[_Field] = field(default_factory=list)

    def named(self, label: str) -> _Field | None:
        """The first field named ``label`` (case-insensitive), or ``None``."""
        for item in self.fields:
            if item.label.lower() == label.lower():
                return item
        return None


class _Doc:
    """A parsed body: lines, fences, headings, sections and harness steps."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.lines: list[_Line] = []
        fence: tuple[str, int] | None = None
        offset = 0
        while offset < len(body):
            newline = body.find("\n", offset)
            end = len(body) if newline == -1 else newline + 1
            text = body[offset : end - 1] if newline != -1 else body[offset:end]
            match = _FENCE_RE.match(text)
            if (
                fence is None
                and match
                and not (match.group(1)[0] == "`" and "`" in match.group(2))
            ):
                fence = (match.group(1)[0], len(match.group(1)))
                self.lines.append(_Line(offset, end, text, True))
            elif fence is not None:
                closes = (
                    match is not None
                    and match.group(1)[0] == fence[0]
                    and len(match.group(1)) >= fence[1]
                    and not match.group(2).strip()
                )
                self.lines.append(_Line(offset, end, text, True))
                if closes:
                    fence = None
            else:
                self.lines.append(_Line(offset, end, text, False))
            offset = end
        self._starts = [line.start for line in self.lines]
        # Headings of level 2 and 3 outside fences: (line index, level, title).
        self.headings: list[tuple[int, int, str]] = []
        for index, line in enumerate(self.lines):
            if line.fenced:
                continue
            match = _HEADING_RE.match(line.text)
            if match:
                self.headings.append(
                    (index, len(match.group(1)), _heading_title(match.group(2)))
                )
        self.steps_range = self._section(lambda title: title.lower() == "steps")
        self.steps: list[_Step] = []
        self.harness = False
        if self.steps_range is not None:
            first, last = self.steps_range
            self.harness = any(
                not line.fenced and re.match(r"^\s*- \*\*Files:\*\*", line.text)
                for line in self.lines[first:last]
            )
        if self.harness:
            self._parse_steps()

    def line_at(self, offset: int) -> _Line:
        """The line holding ``offset`` (the last line past the end)."""
        starts = self._starts
        return self.lines[max(bisect.bisect_right(starts, offset) - 1, 0)]

    # -- sections --------------------------------------------------------------

    def sections(self, predicate: Any, level: int = 2) -> list[tuple[int, int]]:
        """Line ranges ``[heading, end)`` of every heading at ``level`` matching ``predicate``.

        The range ends at the next heading of level ``level`` or less.
        """
        found = []
        end = len(self.lines)
        # One backwards pass: ``end`` is the next heading of level <= ``level``.
        for index, lvl, title in reversed(self.headings):
            if lvl == level and predicate(title):
                found.append((index, end))
            if lvl <= level:
                end = index
        found.reverse()
        return found

    def _section(self, predicate: Any) -> tuple[int, int] | None:
        """The first level-2 section matching ``predicate``, or ``None``."""
        found = self.sections(predicate)
        return found[0] if found else None

    def heading_lines(self) -> set[int]:
        """Indices of the heading lines."""
        return {index for index, _, _ in self.headings}

    # -- harness steps ---------------------------------------------------------

    def _parse_steps(self) -> None:
        """Find the numbered steps under ``## Steps`` and their fields."""
        assert self.steps_range is not None
        first, last = self.steps_range
        heading_lines = sorted(self.heading_lines())
        starts: list[tuple[int, int, str]] = []
        for index in range(first + 1, last):
            line = self.lines[index]
            if line.fenced:
                continue
            match = _STEP_RE.match(line.text)
            if match:
                starts.append((index, int(match.group(1)), match.group(2).strip()))
        for pos, (index, k, name) in enumerate(starts):
            end = starts[pos + 1][0] if pos + 1 < len(starts) else last
            after = bisect.bisect_right(heading_lines, index)
            if after < len(heading_lines) and heading_lines[after] < end:
                end = heading_lines[after]
            step = _Step(k=k, name=name, first=index, last=end)
            step.fields = self._fields(index + 1, end)
            self.steps.append(step)

    def _fields(self, first: int, last: int) -> list[_Field]:
        """The ``- **Label:** value`` fields in lines ``[first, last)``, with continuations."""
        fields_found: list[_Field] = []
        index = first
        while index < last:
            line = self.lines[index]
            match = None if line.fenced else _FIELD_RE.match(line.text)
            if match is None:
                index += 1
                continue
            start = line.start + match.end()
            end = line.text_end
            follow = index + 1
            while follow < last:
                nxt = self.lines[follow]
                if (
                    not nxt.text.strip()
                    or not nxt.text[:1].isspace()
                    or _FIELD_RE.match(nxt.text)
                ):
                    break
                end = nxt.text_end
                follow += 1
            fields_found.append(_Field(match.group(1).strip(), start, end, index))
            index = follow
        return fields_found

    # -- lists -----------------------------------------------------------------

    def items(self, first: int, last: int) -> list[tuple[int, int, int]]:
        """Top-level list items in lines ``[first, last)``: ``(text start, end, line)``.

        An item runs over the indented, non-blank lines that follow it.
        """
        found = []
        index = first
        while index < last:
            line = self.lines[index]
            match = None if line.fenced else _ITEM_RE.match(line.text)
            if match is None:
                index += 1
                continue
            start = line.start + match.end()
            end = line.text_end
            follow = index + 1
            while follow < last:
                nxt = self.lines[follow]
                if (
                    nxt.fenced
                    or not nxt.text.strip()
                    or not nxt.text[:1].isspace()
                    or _ITEM_RE.match(nxt.text.lstrip())
                ):
                    break
                end = nxt.text_end
                follow += 1
            found.append((start, end, index))
            index = follow
        return found


class _Echo:
    """Merged echo intervals, for fast overlap and containment tests."""

    def __init__(self, spans: Sequence[tuple[int, int, str]]) -> None:
        merged: list[list[int]] = []
        for start, end, _ in sorted(spans):
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        self.starts = [a for a, _ in merged]
        self.intervals = [(a, b) for a, b in merged]

    def contains(self, pos: int) -> bool:
        """Whether offset ``pos`` lies inside echo text."""
        i = bisect.bisect_right(self.starts, pos) - 1
        return i >= 0 and self.intervals[i][0] <= pos < self.intervals[i][1]

    def overlaps(self, start: int, end: int) -> bool:
        """Whether ``[start, end)`` overlaps echo text."""
        i = bisect.bisect_right(self.starts, end - 1) - 1
        while i >= 0:
            a, b = self.intervals[i]
            if b <= start:
                return False
            if a < end:
                return True
            i -= 1
        return False

    def scrub(self, body: str, start: int, end: int) -> str:
        """``body[start:end]`` with each echo part written ``"…"``."""
        out = []
        pos = start
        first = max(bisect.bisect_right(self.starts, start) - 1, 0)
        for a, b in self.intervals[first:]:
            if a >= end:
                break
            if b <= pos:
                continue
            if a > pos:
                out.append(body[pos:a])
            out.append('"…"')
            pos = max(pos, b)
        if pos < end:
            out.append(body[pos:end])
        return "".join(out)


# --- echo ------------------------------------------------------------------------


def _purpose_quote_end(value: str) -> int:
    """End of the quoted constraint at the start of a ``Changes on purpose`` value.

    The quote closes at the last ``"`` before `` - <reason>`` (after an
    optional basis tag); without such a ``"`` the whole value is the quote.
    Each candidate is checked in a bounded window, so the scan stays linear.
    """
    cuts = [m.start() for m in re.finditer(r"\s-\s", value)]
    for cut in [*cuts, len(value)]:
        end = cut
        while end > 0 and value[end - 1] in " \t":
            end -= 1
        tag = _TAG_AT_END_RE.search(value, max(0, end - 260), end)
        if tag is not None and not tag.group(0).startswith("(external)"):
            end = tag.start()
            while end > 0 and value[end - 1] in " \t":
                end -= 1
        if end > 1 and value[end - 1] == '"' and not _escaped(value, end - 1):
            return end
    return len(value)


def echo_spans(body: str) -> list[tuple[int, int, str]]:
    """The echo text of a plan body (plan 61 section 3.1, kinds ``a``-``g``).

    Echo text is what a harness plan copies from WhyGraph's own output or
    from outside sources. It is never used for linking, hints or the
    digest:

    * ``a`` - the value of a step's ``**Constraints to preserve:**`` or
      ``**Risks:**`` field;
    * ``b`` - a double-quoted span followed on its line by a basis tag
      (``(quoted: ...)``, ``(cited: ...)``, ``(inferred)``, ``(note #n)``),
      from the first unescaped ``"`` to the last one before the tag;
    * ``c`` - the whole ``## Constraint checks`` section;
    * ``d`` - ``**Constraint at risk:**`` lines under ``## Blockers``;
    * ``e`` - a step field value, or a ``## Risks called out`` list item,
      whose text starts with ``"`` (only the quote of a
      ``**Changes on purpose:**`` value; its reason is the plan's own);
    * ``f`` - a line ending in ``(external)``;
    * ``g`` - the whole ``## Prior art`` section of a harness plan.

    Parameters
    ----------
    body : str
        The ``\\n``-normalised, redacted plan body.

    Returns
    -------
    list of tuple of (int, int, str)
        ``(start, end, kind)`` offsets into ``body``, sorted; spans may
        overlap.
    """
    doc = _Doc(body)
    spans: list[tuple[int, int, str]] = []
    for step in doc.steps:
        for item in step.fields:
            label = item.label.lower()
            value = body[item.start : item.end]
            if label in ("constraints to preserve", "risks"):
                spans.append((item.start, item.end, "a"))
            elif value.startswith('"'):
                if label == "changes on purpose":
                    spans.append(
                        (item.start, item.start + _purpose_quote_end(value), "e")
                    )
                else:
                    spans.append((item.start, item.end, "e"))
    for line in doc.lines:
        pos = 0
        for match in _TAG_RE.finditer(line.text):
            quotes = _unescaped_quotes(line.text[pos : match.start()])
            if len(quotes) >= 2:
                spans.append(
                    (
                        line.start + pos + quotes[0],
                        line.start + pos + quotes[-1] + 1,
                        "b",
                    )
                )
            pos = match.end()
        if line.text.rstrip().endswith("(external)"):
            spans.append((line.start, line.text_end, "f"))

    def whole(predicate: Any, kind: str) -> None:
        for first, last in doc.sections(predicate):
            end = doc.lines[last - 1].end if last > first else doc.lines[first].end
            spans.append((doc.lines[first].start, end, kind))

    whole(lambda title: title.lower().startswith("constraint checks"), "c")
    if doc.harness:
        whole(lambda title: title.lower().startswith("prior art"), "g")
    for first, last in doc.sections(lambda title: title.lower().startswith("blockers")):
        for line in doc.lines[first + 1 : last]:
            if not line.fenced and "**Constraint at risk:**" in line.text:
                spans.append((line.start, line.text_end, "d"))
    for first, last in doc.sections(
        lambda title: title.lower().startswith("risks called out")
    ):
        for start, end, _ in doc.items(first + 1, last):
            if body.startswith('"', start):
                spans.append((start, end, "e"))
    return sorted(spans)


# --- outline ----------------------------------------------------------------------


def _split(body: str, start: int, end: int) -> list[tuple[int, int, bool]]:
    """Cut ``[start, end)`` into parts of at most :data:`SECTION_MAX_CHARS` at line ends.

    A line longer than a part is cut (the part's flag is ``True``) at
    ``SECTION_MAX_CHARS - len(LINE_CUT_NOTICE)``, so the part plus the
    read-time notice still fits; the rest of the line starts the next part.
    """
    parts: list[tuple[int, int, bool]] = []
    pos = start
    room = SECTION_MAX_CHARS - len(LINE_CUT_NOTICE)
    while end - pos > SECTION_MAX_CHARS:
        newline = body.rfind("\n", pos, pos + SECTION_MAX_CHARS)
        if newline != -1:
            # A following line that cannot fit a part of its own is cut
            # anyway: cut it here, after the short lines before it.
            following = body.find("\n", newline + 1, end)
            following = end if following == -1 else following + 1
            if following - (newline + 1) > SECTION_MAX_CHARS:
                newline = -1
        if newline == -1:
            parts.append((pos, pos + room, True))
            pos += room
        else:
            parts.append((pos, newline + 1, False))
            pos = newline + 1
    parts.append((pos, end, False))
    return parts


def build_outline(body: str, spans: Sequence[tuple[int, int, str]]) -> list[dict]:
    """The outline of a plan body: its sections and harness steps (section 3.5).

    One entry per level-2 or level-3 ATX heading outside a code fence, in
    order, numbered from 1; entry 0 ("Header") is the text before the
    first. In the harness format each numbered step under ``## Steps``
    starts its own level-3 entry ``Step <k>: <name>`` whose parent is the
    ``## Steps`` entry. An entry owns the text from its line to the next
    entry. An entry longer than :data:`SECTION_MAX_CHARS` is split at line
    ends into ``(continued i/n)`` parts; a single longer line is cut (the
    entry's ``cut`` flag; :data:`LINE_CUT_NOTICE` is added at read time).
    At most :data:`OUTLINE_MAX_ENTRIES` entries: the last kept section
    absorbs the text after it, up to the cap, and the rest is left out.

    Parameters
    ----------
    body : str
        The whole ``\\n``-normalised plan body, echo included.
    spans : sequence of tuple of (int, int, str)
        :func:`echo_spans` of ``body``; they only set each entry's ``echo``
        flag and never move an offset.

    Returns
    -------
    list of dict
        Entries ``{"n", "level", "title", "start", "end", "parent", "part",
        "echo", "cut"}``: offsets into ``body``; ``parent`` the level-2
        entry's ``n`` for a level-3 entry, else ``None``; ``part`` ``[i, n]``
        for a split entry, else ``None``. Joining every entry's
        ``body[start:end]`` reproduces ``body`` unless text was left out.
    """
    doc = _Doc(body)
    echo = _Echo(spans)
    boundaries: list[tuple[int, int, str]] = []
    step_lines = {step.first: step for step in doc.steps}
    heading_at = {index: (level, title) for index, level, title in doc.headings}
    for index in sorted(set(heading_at) | set(step_lines)):
        if index in heading_at:
            level, title = heading_at[index]
        else:
            step = step_lines[index]
            level, title = 3, f"Step {step.k}: {step.name}"
        boundaries.append((doc.lines[index].start, level, title))
    raw: list[tuple[int, int, str]] = [(0, 1, "Header"), *boundaries]

    entries: list[dict] = []
    parent: int | None = None
    for pos, (start, level, title) in enumerate(raw):
        end = raw[pos + 1][0] if pos + 1 < len(raw) else len(body)
        parts = _split(body, start, end)
        remaining = OUTLINE_MAX_ENTRIES - len(entries)
        last_section = pos + 1 >= len(raw)
        if not last_section and len(parts) >= remaining:
            parts = _split(body, start, len(body))
            last_section = True
        if level == 2:
            parent = len(entries)
        entry_parent = parent if level == 3 else None
        total = len(parts)
        for i, (part_start, part_end, cut) in enumerate(parts[:remaining], start=1):
            suffix = f" (continued {i}/{total})" if i > 1 else ""
            name = _clip(title, OUTLINE_TITLE_MAX - len(suffix)) + suffix
            entries.append(
                {
                    "n": len(entries),
                    "level": level,
                    "title": name,
                    "start": part_start,
                    "end": part_end,
                    "parent": entry_parent,
                    "part": [i, total] if total > 1 else None,
                    "echo": echo.overlaps(part_start, part_end),
                    "cut": cut,
                }
            )
        if last_section:
            break
    return entries


def entry_at(outline: Sequence[dict], offset: int) -> int | None:
    """The ``n`` of the outline entry holding ``offset``.

    Parameters
    ----------
    outline : sequence of dict
        A :func:`build_outline` result (or the stored JSON, decoded).
    offset : int
        An offset into the body.

    Returns
    -------
    int or None
        The entry number; ``None`` when ``offset`` lies past the last entry
        (text the outline left out) or the outline is empty.
    """
    return _entry_finder(outline)(offset)


def _entry_finder(outline: Sequence[dict]) -> Any:
    """:func:`entry_at` for many offsets of one outline (the starts are indexed once)."""
    starts = [entry["start"] for entry in outline]

    def find(offset: int) -> int | None:
        # An empty entry shares its start with the next one; bisect_right picks
        # the last entry starting at or before the offset, so the empty one loses.
        i = bisect.bisect_right(starts, offset) - 1
        if i < 0:
            return None
        entry = outline[i]
        return entry["n"] if offset < entry["end"] else None

    return find


# --- shared readers ------------------------------------------------------------------


def _split_cells(line: str) -> list[str]:
    """The cells of a Markdown table row (``\\|`` does not split)."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", text)]


@dataclass
class _Row:
    """One data row of a ``Resolved decisions`` table."""

    start: int
    end: int
    cells: list[str]
    header: list[str]
    header_line: int
    table: int


def _decision_rows(doc: _Doc) -> list[_Row]:
    """Rows of the tables under every ``resolved decisions`` heading.

    A level-2 heading covers its ``###`` children too, except a child about
    what changed or about risks; tables inside a code fence are ignored.
    """
    rows: list[_Row] = []
    table_no = 0
    seen: set[int] = set()
    for level in (2, 3):
        for first, last in doc.sections(
            lambda title: re.search(r"resolved decisions", title, re.I) is not None,
            level=level,
        ):
            skip = False
            index = first + 1
            while index < last:
                if index in seen:
                    index += 1
                    continue
                line = doc.lines[index]
                heading = None if line.fenced else _HEADING_RE.match(line.text)
                if heading:
                    skip = bool(
                        re.search(r"what changed|\brisks?\b", heading.group(2), re.I)
                    )
                    index += 1
                    continue
                if skip or line.fenced or not line.text.lstrip().startswith("|"):
                    index += 1
                    continue
                block = index
                while (
                    block < last
                    and not doc.lines[block].fenced
                    and doc.lines[block].text.lstrip().startswith("|")
                ):
                    block += 1
                seen.update(range(index, block))
                if block - index >= 2 and _SEPARATOR_RE.match(
                    doc.lines[index + 1].text
                ):
                    table_no += 1
                    header = _split_cells(doc.lines[index].text)
                    for row_index in range(index + 2, block):
                        row_line = doc.lines[row_index]
                        if _SEPARATOR_RE.match(row_line.text):
                            continue
                        rows.append(
                            _Row(
                                start=row_line.start,
                                end=row_line.text_end,
                                cells=_split_cells(row_line.text),
                                header=header,
                                header_line=index,
                                table=table_no,
                            )
                        )
                index = block
    rows.sort(key=lambda row: row.start)
    return rows


def _column(header: list[str], names: tuple[str, ...]) -> int | None:
    """Index of the first header cell named one of ``names`` (case-insensitive)."""
    for i, cell in enumerate(header):
        if cell.strip("* ").lower() in names:
            return i
    return None


def _superseded(row: _Row) -> bool:
    """Whether the row's decision cell starts with ``Superseded`` (bold or not)."""
    col = _column(row.header, ("decision", "answer", "outcome"))
    if col is None:
        col = len(row.header) - 1
    if col >= len(row.cells):
        return False
    return row.cells[col].lstrip("* ").lower().startswith("superseded")


def _header_field(doc: _Doc, label: str) -> tuple[int, int] | None:
    """``(start, end)`` of the value of the first ``**<label>:**`` line, or ``None``."""
    prefix = f"**{label}:**"
    for line in doc.lines:
        if line.fenced:
            continue
        stripped = line.text.lstrip()
        if stripped.startswith(prefix):
            start = line.start + (len(line.text) - len(stripped)) + len(prefix)
            while start < line.text_end and doc.body[start] in " \t":
                start += 1
            return start, line.text_end
    return None


def _non_goals_lines(doc: _Doc) -> list[_Line]:
    """Every line that starts ``**Non-goals:**``."""
    return [
        line
        for line in doc.lines
        if not line.fenced and line.text.lstrip().startswith("**Non-goals:**")
    ]


def _log_section(doc: _Doc) -> tuple[int, int] | None:
    """The ``## Implementation log`` section's line range, or ``None``."""
    return doc._section(lambda title: title.lower().startswith("implementation log"))


def _log_marks(doc: _Doc) -> dict[int, bool] | None:
    """The last ``- [x]`` / ``- [ ]`` mark per step in the log; ``None`` with no log."""
    section = _log_section(doc)
    if section is None:
        return None
    marks: dict[int, bool] = {}
    for line in doc.lines[section[0] + 1 : section[1]]:
        match = None if line.fenced else _LOG_STEP_RE.match(line.text)
        if match:
            marks[int(match.group(2))] = match.group(1) in "xX"
    return marks


# --- why index -------------------------------------------------------------------------


def _parse_files(value: str) -> list[dict]:
    """The ``Files:`` entries of a step: ``[{"path", "ranges", "points"}]``, merged per path."""
    merged: dict[str, dict] = {}
    for piece in value.split(","):
        piece = piece.strip().strip("`").strip()
        while piece.startswith("./"):
            piece = piece[2:]
        match = _FILES_ENTRY_RE.match(piece)
        if match is None or any(
            part in {".", ".."} for part in match.group(1).split("/")
        ):
            continue
        path = match.group(1)
        if path not in merged:
            if len(merged) >= MAX_STEP_FILES:
                continue
            merged[path] = {"path": path, "ranges": [], "points": []}
        entry = merged[path]
        a, b = match.group(2), match.group(3)
        if a is not None and b is not None:
            low, high = sorted((int(a), int(b)))
            if [low, high] not in entry["ranges"]:
                entry["ranges"].append([low, high])
        elif a is not None and int(a) not in entry["points"]:
            entry["points"].append(int(a))
    return list(merged.values())


def build_why_index(
    body: str, outline: Sequence[dict], spans: Sequence[tuple[int, int, str]]
) -> dict:
    """The declared changes and decision rows of a plan (section 3.7).

    Read at scan time and stored in ``Plan.why_index``. It never reads text
    inside ``spans`` and never a risks or "what changed" table.

    Parameters
    ----------
    body : str
        The whole ``\\n``-normalised plan body.
    outline : sequence of dict
        :func:`build_outline` of ``body``.
    spans : sequence of tuple of (int, int, str)
        :func:`echo_spans` of ``body``.

    Returns
    -------
    dict
        ``{"steps", "non_goals", "decisions"}``. ``steps`` (harness format
        only, at most :data:`MAX_WHY_STEPS`): ``{"k", "name", "n", "files",
        "not", "on_purpose", "done"}`` with ``files`` items ``{"path",
        "ranges", "points"}`` from the step's ``**Files:**`` line, ``not``
        the backtick identifiers of its ``**Boundaries:**`` value and
        ``done`` its last implementation-log mark (``None`` without one).
        ``non_goals``: backtick identifiers of the ``**Non-goals:**`` line.
        ``decisions`` (at most :data:`MAX_WHY_DECISIONS`): ``{"id",
        "title", "n", "names"}`` per row of a table under a ``resolved
        decisions`` heading; ``title`` comes only from a ``Question`` or
        ``Topic`` column and is left out without one.
    """
    doc = _Doc(body)
    echo = _Echo(spans)
    steps: list[dict] = []
    marks = _log_marks(doc) if doc.harness else None
    for step in doc.steps[:MAX_WHY_STEPS]:
        files_field = step.named("Files")
        files = (
            _parse_files(body[files_field.start : files_field.end])
            if files_field is not None and not echo.contains(files_field.start)
            else []
        )
        boundaries = step.named("Boundaries")
        names = (
            _identifiers(echo.scrub(body, boundaries.start, boundaries.end))
            if boundaries is not None
            else []
        )
        steps.append(
            {
                "k": step.k,
                "name": _clip(step.name, _HINT_TITLE_MAX),
                "n": entry_at(outline, doc.lines[step.first].start),
                "files": files,
                "not": names,
                "on_purpose": step.named("Changes on purpose") is not None,
                "done": None if marks is None else marks.get(step.k),
            }
        )
    non_goals: list[str] = []
    for line in _non_goals_lines(doc):
        for name in _identifiers(echo.scrub(body, line.start, line.text_end)):
            if name not in non_goals:
                non_goals.append(name)
    decisions: list[dict] = []
    for k, row in enumerate(_decision_rows(doc), start=1):
        if len(decisions) >= MAX_WHY_DECISIONS:
            break
        if echo.contains(row.start):
            continue
        first = row.cells[0] if row.cells else ""
        words = first.replace("*", "").split()
        row_id = words[0] if words and _ROW_ID_RE.fullmatch(words[0]) else f"row {k}"
        item: dict[str, Any] = {"id": row_id}
        col = _column(row.header, ("question", "topic"))
        if col is not None and col < len(row.cells):
            item["title"] = _clip(row.cells[col], _HINT_TITLE_MAX)
        item["n"] = entry_at(outline, row.start)
        item["names"] = _identifiers(echo.scrub(body, row.start, row.end))
        decisions.append(item)
    return {"steps": steps, "non_goals": non_goals, "decisions": decisions}


# --- path mentions ------------------------------------------------------------------------


@dataclass(frozen=True)
class PathMention:
    """One repo path a plan names outside echo text.

    Attributes
    ----------
    path : str
        The repo-relative path (a ``:line`` suffix, a leading ``./`` and
        trailing ``.,;:)`` removed).
    sections : tuple of int
        Outline entries that mention it, ascending; never entry 0 or the
        ``## Working set`` entry.
    declared : bool
        Whether a step's ``**Files:**`` line names it with a range or point.
    """

    path: str
    sections: tuple[int, ...]
    declared: bool


def _negative_regions(doc: _Doc) -> list[tuple[int, int, str]]:
    """Text whose paths make no link: Non-goals, Boundaries, "No change needed", Amendments ``<old>``."""
    regions: list[tuple[int, int, str]] = [
        (line.start, line.text_end, "non-goals") for line in _non_goals_lines(doc)
    ]
    for step in doc.steps:
        for item in step.fields:
            if item.label.lower() == "boundaries":
                regions.append((item.start, item.end, "boundaries"))
    fenced = {line.start for line in doc.lines if line.fenced}
    for match in _NO_CHANGE_RE.finditer(doc.body):
        if doc.line_at(match.start()).start in fenced:
            continue
        # A sentence is bounded, so many openers cannot make this quadratic.
        limit = min(len(doc.body), match.end() + _SENTENCE_MAX)
        end_match = _SENTENCE_END_RE.search(doc.body, match.end(), limit)
        end = end_match.start() if end_match else limit
        regions.append((match.start(), end, "no-change"))
    for first, last in doc.sections(
        lambda title: title.lower().startswith("amendments")
    ):
        for line in doc.lines[first + 1 : last]:
            arrow = line.text.find(" -> ")
            if not line.fenced and arrow != -1:
                regions.append((line.start, line.start + arrow, "amendment-old"))
    return regions


def path_mentions(
    body: str, outline: Sequence[dict], spans: Sequence[tuple[int, int, str]]
) -> list[PathMention]:
    """Every repo-path-shaped token of a plan outside echo and negative text.

    A token matches ``[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+`` (a ``:<line>``
    or ``:<a>-<b>`` suffix is dropped; a leading ``./`` and trailing
    ``.``, ``,``, ``;``, ``:`` and ``)`` are stripped). Tokens inside echo
    text (``spans``), in the ``**Non-goals:**`` line, in a step's
    ``**Boundaries:**`` value, in a sentence that starts ``No change
    needed`` or on the ``<old>`` side of an ``## Amendments`` entry are
    skipped. The caller keeps only the paths that are blobs at ``HEAD``.

    Parameters
    ----------
    body : str
        The whole ``\\n``-normalised plan body.
    outline : sequence of dict
        :func:`build_outline` of ``body`` (or the stored outline).
    spans : sequence of tuple of (int, int, str)
        :func:`echo_spans` of ``body``.

    Returns
    -------
    list of PathMention
        One per distinct path, in order of first mention.
    """
    doc = _Doc(body)
    echo = _Echo(spans)
    negative = _Echo(_negative_regions(doc))
    excluded = {
        entry["n"]
        for entry in outline
        if entry["n"] == 0
        or (entry["level"] == 2 and re.match(r"working set\b", entry["title"], re.I))
    }
    declared: set[str] = set()
    for step in doc.steps:
        files_field = step.named("Files")
        if files_field is None or echo.contains(files_field.start):
            continue
        for entry in _parse_files(body[files_field.start : files_field.end]):
            if entry["ranges"] or entry["points"]:
                declared.add(entry["path"])
    found: dict[str, set[int]] = {}
    section_of = _entry_finder(outline)
    for match in _PATH_RE.finditer(body):
        start = match.start()
        if echo.contains(start) or negative.contains(start):
            continue
        path = match.group(1)
        while path.startswith("./"):
            path = path[2:]
        path = path.rstrip(".,;:)")
        if "/" not in path or any(part in {"", ".", ".."} for part in path.split("/")):
            continue
        sections = found.setdefault(path, set())
        n = section_of(start)
        if n is not None and n not in excluded:
            sections.add(n)
    return [
        PathMention(
            path=path, sections=tuple(sorted(sections)), declared=path in declared
        )
        for path, sections in found.items()
    ]


# --- digest ----------------------------------------------------------------------------------


@dataclass
class _Item:
    """One droppable digest line and the outline entry it came from."""

    text: str
    n: int | None


def _log_fact(doc: _Doc) -> str | None:
    """The implementation-log fact line, or ``None`` with no log."""
    section = _log_section(doc)
    if section is None:
        return None
    done: set[int] = set()
    acceptance: str | None = None
    for line in doc.lines[section[0] + 1 : section[1]]:
        if line.fenced:
            continue
        step = _LOG_STEP_RE.match(line.text)
        if step and step.group(1) in "xX":
            done.add(int(step.group(2)))
        accepted = _ACCEPTANCE_RE.match(line.text)
        if accepted:
            acceptance = accepted.group(1).strip()[:300]
    count = f"{len(done)} of {len(doc.steps)} steps done."
    if acceptance is None:
        return f"Implementation log: {count}"
    date = _DATE_TAIL_RE.search(acceptance)
    outcome = acceptance[: date.start()] if date else acceptance
    outcome = _clip(outcome, 200)
    when = f" ({date.group(1)})" if date else ""
    return f"Implementation log: acceptance {outcome}{when}; {count}"


def _changes_on_purpose(doc: _Doc, outline: Sequence[dict]) -> list[_Item]:
    """One line per step with a ``**Changes on purpose:**`` field; the quote becomes its tag."""
    items = []
    for step in doc.steps:
        found = step.named("Changes on purpose")
        if found is None:
            continue
        value = doc.body[found.start : found.end]
        tag = ""
        reason = value
        if value.startswith('"'):
            end = _purpose_quote_end(value)
            tail = _PURPOSE_TAIL_RE.match(value, end)
            if tail and tail.group(1):
                tag = f" {tail.group(1)}"
            reason = value[tail.end() :] if tail else value[end:]
        reason = _clip(reason.lstrip("- ").strip(), _DIGEST_FIELD_MAX)
        name = _clip(step.name, _HINT_TITLE_MAX)
        line = f"Step {step.k} ({name}) changes a recorded constraint{tag}"
        items.append(
            _Item(
                f"{line}: {reason}" if reason else line,
                entry_at(outline, doc.lines[step.first].start),
            )
        )
    return items


def _open_questions(doc: _Doc, echo: _Echo, outline: Sequence[dict]) -> list[_Item]:
    """``Q<n>: <question> Default: <option>`` for each open question with a default."""
    items = []
    for first, last in doc.sections(
        lambda title: title.lower().startswith("open questions")
    ):
        for start, end, _ in doc.items(first + 1, last):
            if echo.overlaps(start, end):
                continue
            text = " ".join(doc.body[start:end].split())
            marker = text.find("**Default:**")
            if marker == -1:
                continue
            after = marker + len("**Default:**")
            label = _LABEL_RE.search(text, after)
            default = text[after : label.start() if label else len(text)].strip()
            if not default:
                continue
            ident = _QUESTION_ID_RE.match(text)
            question = text[ident.end() :] if ident else text
            question = _LABEL_RE.split(question, maxsplit=1)[0].strip()
            prefix = f"{ident.group(1) or ident.group(2)}: " if ident else ""
            items.append(
                _Item(
                    f"{prefix}{_clip(question, 200)} Default: {_clip(default, 120)}",
                    entry_at(outline, start),
                )
            )
            if len(items) >= _DIGEST_QUESTIONS:
                return items
    return items


def _own_risks(doc: _Doc, echo: _Echo, outline: Sequence[dict]) -> list[_Item]:
    """Bullets of ``## Risks called out`` in the plan's own words (no quote, no tag)."""
    items = []
    for first, last in doc.sections(
        lambda title: title.lower().startswith("risks called out")
    ):
        for start, end, _ in doc.items(first + 1, last):
            text = doc.body[start:end]
            if (
                text.startswith('"')
                or _TAG_AT_END_RE.search(text)
                or echo.overlaps(start, end)
            ):
                continue
            items.append(
                _Item(f"- {_clip(text, _DIGEST_FIELD_MAX)}", entry_at(outline, start))
            )
            if len(items) >= _DIGEST_RISKS:
                return items
    return items


def _fallback(doc: _Doc, echo: _Echo) -> str:
    """The first 1,500 characters after the title, without the status line or echo."""
    lines: list[str] = []
    size = 0
    started = False
    for line in doc.lines:
        if not started:
            if not line.fenced and _H1_RE.match(line.text):
                started = True
                continue
            if line.text.strip() and not line.text.lstrip().startswith("**"):
                started = True
            else:
                continue
        if line.text.lstrip().startswith("**Status:**"):
            continue
        text = echo.scrub(doc.body, line.start, line.text_end)
        if size + len(text) + 1 > _FALLBACK_CHARS:
            if not lines:
                lines.append(text[:_FALLBACK_CHARS])
            break
        lines.append(text)
        size += len(text) + 1
    return "\n".join(lines).strip("\n")


def build_digest(
    body: str,
    outline: Sequence[dict],
    spans: Sequence[tuple[int, int, str]],
    *,
    stem: str | None = None,
) -> str:
    """The decision digest of a plan (section 3.4), at most 3,000 characters.

    Harness format (a ``## Steps`` section with ``- **Files:**`` lines), in
    order: the title; the ``**Objective:**`` and ``**Non-goals:**`` values
    (each clipped at 400; never the plan's own ``**Status:**`` or ``**Read
    first:**``); one implementation-log fact line; the ``Resolved
    decisions`` rows (``Superseded`` rows skipped); one line per ``Changes
    on purpose`` step with the quote replaced by its tag; up to 3 open
    questions with a default; up to 3 risks in the plan's own words; a last
    line with the section pointer. Any other format: the title, the
    ``Objective`` / ``Goal`` / ``Non-goals`` lines and the decision rows,
    or else the first 1,500 characters; then the last line. Over the cap,
    items are dropped whole in reverse order (risks first; decision rows
    last, earliest first) and the last line says how many, and where.
    Echo text (``spans``) is never included.

    Parameters
    ----------
    body : str
        The whole ``\\n``-normalised plan body.
    outline : sequence of dict
        :func:`build_outline` of ``body``.
    spans : sequence of tuple of (int, int, str)
        :func:`echo_spans` of ``body``.
    stem : str, optional
        The plan's stem, for the section URI of the last line (``<stem>``
        when not given).

    Returns
    -------
    str
        The digest.
    """
    doc = _Doc(body)
    echo = _Echo(spans)
    title = parse_header(body)[0] or (stem or "Untitled plan")
    fixed = [f"# {title}"]

    def header_line(label: str) -> None:
        found = _header_field(doc, label)
        if found is None:
            return
        text = _clip(echo.scrub(body, *found), _DIGEST_FIELD_MAX)
        if text:
            fixed.append(f"**{label}:** {text}")

    if doc.harness:
        header_line("Objective")
        header_line("Non-goals")
        fact = _log_fact(doc)
        if fact:
            fixed.append(fact)
    else:
        for label in ("Objective", "Goal", "Non-goals"):
            header_line(label)

    rows = [
        row
        for row in _decision_rows(doc)
        if not _superseded(row) and not echo.contains(row.start)
    ]
    headers: dict[int, tuple[str, str]] = {}
    for row in rows:
        if row.table not in headers:
            first = doc.lines[row.header_line]
            sep = doc.lines[row.header_line + 1]
            headers[row.table] = (first.text.strip(), sep.text.strip())
    decisions = [
        (
            row.table,
            _Item(
                echo.scrub(body, row.start, row.end).strip(),
                entry_at(outline, row.start),
            ),
        )
        for row in rows
    ]
    changes = _changes_on_purpose(doc, outline) if doc.harness else []
    questions = _open_questions(doc, echo, outline) if doc.harness else []
    risks = _own_risks(doc, echo, outline) if doc.harness else []
    fallback = ""
    if not doc.harness and len(fixed) == 1 and not decisions:
        fallback = _fallback(doc, echo)

    lines_total = line_count(body)
    uri = f"whygraph://plan/{stem or '<stem>'}/section/<n>"
    dropped: dict[str, tuple[int, int | None]] = {}

    def drop(kind: str, item: _Item) -> None:
        # Report the earliest outline entry that holds a dropped item.
        count, n = dropped.get(kind, (0, None))
        if item.n is not None and (n is None or item.n < n):
            n = item.n
        dropped[kind] = (count + 1, n)

    def last_line() -> str:
        parts = []
        for kind, noun in (
            ("decisions", "decision row"),
            ("changes", "change-on-purpose line"),
            ("questions", "open question"),
            ("risks", "risk"),
        ):
            if kind in dropped:
                count, n = dropped[kind]
                where = f" (section {n})" if n is not None else ""
                parts.append(f"{count} {noun}{'' if count == 1 else 's'}{where}")
        left = f"; left out: {', '.join(parts)}" if parts else ""
        return (
            f"… digest of a {lines_total}-line plan{left}; "
            f"read one section at a time: {uri}"
        )

    def render() -> str:
        out = list(fixed)
        if fallback:
            out += ["", fallback]
        table = None
        for table_no, item in decisions:
            if table_no != table:
                out += ["", *headers[table_no]]
                table = table_no
            out.append(item.text)
        if changes:
            out += ["", *(item.text for item in changes)]
        if questions:
            out += ["", "Open questions:", *(item.text for item in questions)]
        if risks:
            out += ["", "Risks:", *(item.text for item in risks)]
        out += ["", last_line()]
        return "\n".join(out)

    # Items that cannot fit even on their own go first, in the order the loop
    # below would drop them anyway, so that loop stays short on a huge plan.
    total = sum(len(item.text) + 1 for item in changes)
    while changes and total > DIGEST_MAX_CHARS:
        total -= len(changes[-1].text) + 1
        drop("changes", changes.pop())
    total = sum(len(item.text) + 1 for _, item in decisions)
    first_kept = 0
    while first_kept < len(decisions) and total > DIGEST_MAX_CHARS:
        total -= len(decisions[first_kept][1].text) + 1
        drop("decisions", decisions[first_kept][1])
        first_kept += 1
    decisions = decisions[first_kept:]

    digest = render()
    while len(digest) > DIGEST_MAX_CHARS:
        if risks:
            drop("risks", risks.pop())
        elif questions:
            drop("questions", questions.pop())
        elif changes:
            drop("changes", changes.pop())
        elif decisions:
            # The earliest rows go first: in a long-lived plan the later rows
            # supersede them.
            drop("decisions", decisions.pop(0)[1])
        else:
            return digest[: DIGEST_MAX_CHARS - 1] + "…"
        digest = render()
    return digest


# =====================================================================================
# Read side (plan 61 section 4.5)
# =====================================================================================

PLAN_URI = "whygraph://plan/{stem}"
"""The plan resource's URI template (the digest and the contents list)."""

PLAN_SECTION_URI = "whygraph://plan/{stem}/section/{n}"
"""The section resource's URI template."""

PLAN_READING = "Intent when it was written. Commits and PRs say what was built."
"""The one fixed line both resources carry, so the framing travels with the data."""

COPIES_NOTE = (
    "Quoted lines and the Prior art section are copies of rationale cards, notes, "
    "commits and PRs from when the plan was written. Read the current card or "
    "whygraph_area_history for the current record."
)
"""Set beside ``copies: true`` on a section whose text holds echo."""

STATUS_LINE_REPLACEMENT = (
    "**Status:** [the plan's own line, never updated - see status]"
)
"""What entry 0's own ``**Status:**`` line is served as."""

REF_TITLE_MAX = 80
"""The most characters of a ref's title, a hint's title and a contents title."""

CARD_REFS_MAX = 3
CARD_HINTS_MAX = 2
CARD_PLANS_CHARS = 1200
"""Beside a card: at most this many refs, hints per ref and serialized characters."""

FILE_REFS_MAX = 5
FILE_LINKS_MAX = 3
SECTIONS_MAX = 5
"""File-level answers: refs, ``file`` links among them, ``sections`` per ref."""

RECORD_COMMITS_MAX = 5
RECORD_PRS_MAX = 5
RECORD_PATHS_MAX = 10
CONTENTS_MAX_ENTRIES = 40
CONTENTS_MAX_CHARS = 3000
RECORD_MAX_CHARS = 8500
CHILDREN_MAX = 20
"""Whole-answer bounds of the agent form (section 3.5, criterion 15h)."""

LINK_KINDS = ("commit", "pr", "declared", "file")
"""Strongest first."""

_STATUS_ORDER = {"shipped": 0, "in_flight": 1, "not_implemented": 2}
_STATUS_LINE_RE = re.compile(r"(?m)^\*\*Status:?\*\*:?[^\n]*$")
_CHUNK = 400
_SECTIONS_COLUMN_RE = re.compile(r"\d+")


def _chunks(values: Sequence[Any], size: int = _CHUNK) -> Iterator[Sequence[Any]]:
    """``values`` in slices that stay under SQLite's bound-variable limit."""
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _linked() -> bool:
    """Whether the bound project is linked to a platform (it has no local plans)."""
    ctx = current_project()
    return ctx is not None and ctx.remote is not None


def _under(path: str, plans_dir: str) -> bool:
    """Whether ``path`` lies under the plans folder."""
    base = plans_dir.strip().strip("/")
    return bool(base) and path.startswith(base + "/")


def _oids(raw: str | None) -> set[str]:
    """The commit oids a PR's ``commit_titles`` JSON lists."""
    try:
        entries = json.loads(raw or "[]")
    except ValueError:
        return set()
    if not isinstance(entries, list):
        return set()
    return {
        e["oid"]
        for e in entries
        if isinstance(e, dict) and isinstance(e.get("oid"), str)
    }


def _known_files(raw: str | None, changed_files: int | None) -> list[str] | None:
    """A PR's files when the list is complete (section 3.2), else ``None``.

    Known means: it parses to a non-empty list and ``changed_files`` is not
    null and equals its length. ``[]``, a parse error, a null
    ``changed_files`` and ``changed_files > len(files)`` are unknown.
    """
    try:
        files = json.loads(raw or "[]")
    except ValueError:
        return None
    if not isinstance(files, list) or not files:
        return None
    if changed_files is None or changed_files != len(files):
        return None
    return [f for f in files if isinstance(f, str)]


# --- derived status ---------------------------------------------------------------------


@dataclass
class PlanState:
    """What :func:`derive_status` found for one plan.

    Attributes
    ----------
    status : str
        ``shipped``, ``in_flight`` or ``not_implemented`` (section 3.2).
    implementing : list of dict
        The implementing commits, newest first: ``{"sha", "subject",
        "on_default_branch", "committed_at"}``. Publishing links are not here.
    prs : list of dict
        Every linked PR that has a row, newest number first: ``{"number",
        "title", "state"}`` (``state`` is ``merged`` when ``merged_at`` is set).
    publishing : int
        How many linked commits and PRs only published the plan.
    changed_paths : set of str
        The files the implementing commits and PRs changed (a PR's only
        when its file list is known).
    """

    status: str
    implementing: list[dict] = field(default_factory=list)
    prs: list[dict] = field(default_factory=list)
    publishing: int = 0
    changed_paths: set[str] = field(default_factory=set)

    @property
    def implementing_shas(self) -> set[str]:
        """The SHAs of :attr:`implementing`."""
        return {c["sha"] for c in self.implementing}


def derive_status(
    session: Session, stems: Sequence[str], plans_dir: str
) -> dict[str, PlanState]:
    """Derive the status of ``stems`` at read time (section 3.2), never stored.

    The implementing commits of a plan are the SHAs with a ``commit`` row
    among its ``commit`` links and its ``pr`` links' ``merge_commit_sha`` /
    ``head_sha``. ``shipped``: one of them is on the default branch, or a
    linked PR has ``merged_at``; ``in_flight``: otherwise some implementing
    commit exists or a linked PR is open; else ``not_implemented``.
    **Publishing links do not count**: a commit whose file rows all lie
    under ``plans_dir``; a PR whose *known* files all do; a merge commit
    (it has no file rows) with no PR, or whose PR is a publishing one. A
    commit with no file rows and a PR with unknown files count as
    implementing.

    Parameters
    ----------
    session : Session
        An open WhyGraph session.
    stems : sequence of str
        The plans to derive.
    plans_dir : str
        ``[plans].dir``.

    Returns
    -------
    dict of str to PlanState
        One entry per distinct stem.
    """
    unique = list(dict.fromkeys(stems))
    if not unique:
        return {}
    commit_refs: dict[str, list[str]] = {s: [] for s in unique}
    pr_refs: dict[str, list[int]] = {s: [] for s in unique}
    for chunk in _chunks(unique):
        rows = session.exec(
            select(PlanLink.stem, PlanLink.kind, PlanLink.ref).where(
                col(PlanLink.stem).in_(chunk), col(PlanLink.kind).in_(("commit", "pr"))
            )
        ).all()
        for stem, kind, ref in rows:
            if kind == "commit":
                commit_refs[stem].append(ref)
            elif ref.isdigit():
                pr_refs[stem].append(int(ref))

    numbers = sorted({n for refs in pr_refs.values() for n in refs})
    prs: dict[int, Any] = {}
    for chunk in _chunks(numbers):
        for row in session.exec(
            select(
                PullRequest.number,
                PullRequest.title,
                PullRequest.state,
                PullRequest.merged_at,
                PullRequest.merge_commit_sha,
                PullRequest.head_sha,
                PullRequest.files,
                PullRequest.changed_files,
            ).where(col(PullRequest.number).in_(chunk))
        ).all():
            prs[row.number] = row

    wanted = {sha for refs in commit_refs.values() for sha in refs}
    for pr in prs.values():
        wanted.update(sha for sha in (pr.merge_commit_sha, pr.head_sha) if sha)
    commits: dict[str, Any] = {}
    paths: dict[str, set[str]] = {}
    for chunk in _chunks(sorted(wanted)):
        for row in session.exec(
            select(
                Commit.sha,
                Commit.parent_shas,
                Commit.on_default_branch,
                Commit.subject,
                Commit.committed_at,
            ).where(col(Commit.sha).in_(chunk))
        ).all():
            commits[row.sha] = row
        for sha, path in session.exec(
            select(CommitFileChange.commit_sha, CommitFileChange.path).where(
                col(CommitFileChange.commit_sha).in_(chunk)
            )
        ).all():
            paths.setdefault(sha, set()).add(path)

    # A merge commit has no file rows: the PR it belongs to decides.
    merges = sorted(
        {
            sha
            for refs in commit_refs.values()
            for sha in refs
            if sha in commits
            and not paths.get(sha)
            and len(commits[sha].parent_shas.split()) >= 2
        }
    )
    merge_prs: dict[str, Any] = {}
    for chunk in _chunks(merges):
        for row in session.exec(
            select(
                PullRequest.number,
                PullRequest.merge_commit_sha,
                PullRequest.head_sha,
                PullRequest.files,
                PullRequest.changed_files,
            ).where(
                col(PullRequest.merge_commit_sha).in_(chunk)
                | col(PullRequest.head_sha).in_(chunk)
            )
        ).all():
            for sha in (row.merge_commit_sha, row.head_sha):
                if sha in chunk:
                    merge_prs.setdefault(sha, row)

    def pr_publishes(pr: Any) -> bool:
        files = _known_files(pr.files, pr.changed_files)
        return files is not None and all(_under(f, plans_dir) for f in files)

    def commit_publishes(sha: str) -> bool:
        own = paths.get(sha)
        if own:
            return all(_under(p, plans_dir) for p in own)
        if len(commits[sha].parent_shas.split()) >= 2:
            owner = merge_prs.get(sha)
            return True if owner is None else pr_publishes(owner)
        return False

    out: dict[str, PlanState] = {}
    for stem in unique:
        implementing: dict[str, Any] = {}
        changed: set[str] = set()
        linked_prs: list[dict] = []
        publishing = 0
        merged = opened = False
        for number in sorted(set(pr_refs[stem]), reverse=True):
            pr = prs.get(number)
            if pr is None:
                continue
            linked_prs.append(
                {
                    "number": pr.number,
                    "title": pr.title,
                    "state": "merged" if pr.merged_at else pr.state,
                }
            )
            if pr_publishes(pr):
                publishing += 1
                continue
            merged = merged or pr.merged_at is not None
            opened = opened or (pr.state == "open" and pr.merged_at is None)
            changed.update(_known_files(pr.files, pr.changed_files) or ())
            for sha in (pr.merge_commit_sha, pr.head_sha):
                if sha in commits:
                    implementing[sha] = commits[sha]
        for sha in dict.fromkeys(commit_refs[stem]):
            if sha not in commits:
                continue
            if commit_publishes(sha):
                publishing += 1
            else:
                implementing[sha] = commits[sha]
        for sha in implementing:
            changed.update(paths.get(sha, ()))
        if merged or any(c.on_default_branch == 1 for c in implementing.values()):
            status = "shipped"
        elif implementing or opened:
            status = "in_flight"
        else:
            status = "not_implemented"
        ordered = sorted(
            implementing.values(),
            key=lambda c: (c.committed_at or "", c.sha),
            reverse=True,
        )
        out[stem] = PlanState(
            status=status,
            implementing=[
                {
                    "sha": c.sha,
                    "subject": c.subject,
                    "on_default_branch": bool(c.on_default_branch),
                    "committed_at": c.committed_at,
                }
                for c in ordered
            ],
            prs=linked_prs,
            publishing=publishing,
            changed_paths=changed,
        )
    return out


# --- relevance (section 3.6, 3.7) ---------------------------------------------------------


def card_plan_shas(evidence: Iterable["CommitEvidence"]) -> list[str]:
    """The line-level SHAs of an evidence list: every item whose source is not ``area``.

    The one SHA source beside a card, so the MCP tool and both Explorer
    routes return the same plans. ``area`` items are commits that merely
    touched the file; never ``card["evidence"]`` (capped at 5).

    Parameters
    ----------
    evidence : iterable of CommitEvidence
        ``collect_evidence(target, limit=20)``.

    Returns
    -------
    list of str
        Distinct commit SHAs, in order.
    """
    return list(
        dict.fromkeys(item.commit.sha for item in evidence if item.source != "area")
    )


@dataclass(frozen=True)
class _Anchor:
    """The one symbol a target resolves to (section 3.6)."""

    path: str
    name: str
    qualified_name: str
    start: int
    end: int

    @property
    def negative_names(self) -> set[str]:
        """Spellings a Boundaries / Non-goals name may use for this symbol."""
        qn = self.qualified_name.replace(".", "::")
        return {self.qualified_name, qn, self.name, qn.rsplit("::", 1)[-1]}

    @property
    def decision_names(self) -> set[str]:
        """Spellings a decision row's name must equal (the full qualified name)."""
        return {self.qualified_name, self.qualified_name.replace(".", "::")}


def _file_symbols(path: str) -> list[Any]:
    """The non-``file`` CodeGraph symbols of one file (empty with no index).

    The one ``GraphBackend`` lookup of symbol scope; it never raises.
    """
    from whygraph.services.codegraph import CodeGraph

    from .targets import repo_root

    try:
        with CodeGraph.for_repository(
            repo_root(), codegraph_db=get_config().codegraph_db
        ) as graph:
            return [
                s for s in graph.area(path) if s.kind != "file" and s.file_path == path
            ]
    except Exception:  # noqa: BLE001 - no index, a stale index: no anchor
        _log.debug("plans: no CodeGraph symbols for %s", path, exc_info=True)
        return []


def _innermost(symbols: Sequence[Any], start: int, end: int) -> Any | None:
    """The smallest symbol containing ``[start, end]``."""
    inside = [s for s in symbols if s.start_line <= start and end <= s.end_line]
    if not inside:
        return None
    return min(inside, key=lambda s: (s.end_line - s.start_line, -s.start_line))


def _resolve_anchor(target: "Target", symbols: Sequence[Any]) -> _Anchor | None:
    """The anchor symbol of ``target`` among ``symbols`` (section 3.6)."""
    if target.qualified_name:
        found = [
            s
            for s in symbols
            if s.qualified_name == target.qualified_name and s.file_path == target.path
        ]
        exact = [
            s
            for s in found
            if (s.start_line, s.end_line) == (target.line_start, target.line_end)
        ]
        symbol = (exact or found or [None])[0]
    else:
        symbol = _innermost(symbols, target.line_start, target.line_end)
    if symbol is None:
        return None
    return _Anchor(
        path=symbol.file_path,
        name=symbol.name,
        qualified_name=symbol.qualified_name,
        start=symbol.start_line,
        end=symbol.end_line,
    )


def _step_declares(step: dict, anchor: _Anchor, symbols: Sequence[Any]) -> bool:
    """Whether a step's ``Files:`` entry for the anchor's file declares the anchor.

    A range overlaps the anchor, or a point lies inside it and is not its
    first line; and the anchor is the innermost symbol containing that
    overlap (or point).
    """
    for entry in step.get("files", []):
        if entry.get("path") != anchor.path:
            continue
        spans: list[tuple[int, int, bool]] = []
        for a, b in entry.get("ranges", []):
            low, high = max(a, anchor.start), min(b, anchor.end)
            if low <= high:
                spans.append((low, high, False))
        for point in entry.get("points", []):
            if anchor.start < point <= anchor.end:
                spans.append((point, point, True))
        for low, high, _is_point in spans:
            innermost = _innermost(symbols, low, high)
            if innermost is not None and (
                innermost.qualified_name,
                innermost.start_line,
                innermost.end_line,
            ) == (anchor.qualified_name, anchor.start, anchor.end):
                return True
    return False


def _hints_for(
    why: dict, anchor: _Anchor, symbols: Sequence[Any], changed: set[str]
) -> tuple[list[dict], bool]:
    """The hints a plan gives for ``anchor`` and whether it declares it (section 3.7).

    Returns
    -------
    tuple of (list of dict, bool)
        The hints (steps with ``on_purpose`` first, then decision rows, not
        yet capped) and whether the plan declares a change to the anchor
        (a matching step, or a matching decision row).
    """
    negative = anchor.negative_names
    steps: list[dict] = []
    if anchor.path in changed and not negative & set(why.get("non_goals", [])):
        for step in why.get("steps", []):
            if step.get("done") is False or negative & set(step.get("not", [])):
                continue
            if _step_declares(step, anchor, symbols):
                steps.append(step)
    steps.sort(key=lambda s: not s.get("on_purpose"))
    hints = [
        {
            "id": f"step {s['k']}",
            "title": _clip(s.get("name", ""), REF_TITLE_MAX),
            "n": s["n"],
        }
        for s in steps
    ]
    matched = False
    for row in why.get("decisions", []):
        if anchor.decision_names & set(row.get("names", [])):
            matched = True
            hint: dict = {"id": row["id"]}
            if row.get("title"):
                hint["title"] = _clip(row["title"], REF_TITLE_MAX)
            hint["n"] = row["n"]
            hints.append(hint)
    return hints, bool(steps) or matched


def _sections(raw: str | None) -> list[int]:
    """The outline numbers of a ``PlanLink.sections`` value."""
    return [int(x) for x in _SECTIONS_COLUMN_RE.findall(raw or "")]


def _serialized(refs: list[dict]) -> int:
    """The length of ``refs`` as the tool result serializes the ``plans`` value."""
    return len(json.dumps(refs, ensure_ascii=False))


def _shape_ref(row: Any, link: str, status: str, *, detail: str) -> dict:
    """One :class:`PlanRef` (section 3.3), without ``hints`` / ``sections``."""
    ref: dict[str, Any] = {
        "stem": row.stem,
        "title": _clip(row.title or row.stem, REF_TITLE_MAX),
        "status": status,
        "link": link,
        "uri": PLAN_URI.format(stem=row.stem),
    }
    if not row.present and detail != "full":
        ref["present"] = False
    return ref


def _finish_full(
    ref: dict, row: Any, shas: Sequence[str], implementing: set[str]
) -> None:
    """Add what ``detail="full"`` adds: ``path``, ``present``, ``commits``."""
    ref["path"] = row.path
    ref["present"] = bool(row.present)
    ref["commits"] = [sha[:8] for sha in shas if sha in implementing][
        :RECORD_COMMITS_MAX
    ]


def guarded_plans_for(*args: Any, **kwargs: Any) -> list[dict]:
    """:func:`plans_for` that never raises: a failure logs and answers ``[]``.

    The tools and the Explorer routes attach ``plans`` beside data that
    must still answer when the plans lookup breaks (section 4.6).
    """
    try:
        return plans_for(*args, **kwargs)
    except Exception:
        _log.warning("plans lookup failed", exc_info=True)
        return []


def plans_for(
    shas: Sequence[str],
    paths: Sequence[str],
    *,
    target: "Target | None" = None,
    scope: Literal["symbol", "file"] = "file",
    detail: Literal["compact", "full"] = "compact",
    limit: int | None = None,
) -> list[dict]:
    """The plans that relate to a target, as :class:`PlanRef` dicts (section 3.6).

    **Symbol scope** (beside a card): ``commit`` and ``pr`` links from the
    given line-level SHAs (:func:`card_plan_shas`); ``declared`` only for a
    ``shipped``, ``present`` plan whose step (a built step, an implementing
    change to the file) or decision row names the target's anchor symbol;
    never ``file``. Refs carry ``hints``; at most 3 refs, 2 hints each and
    1,200 serialized characters (hints are dropped from the last ref, then
    the ref). **File scope** (area history): all four links, ``declared`` /
    ``file`` from ``present`` plans only, ``not_implemented`` ranked last,
    at most 5 refs and 3 ``file`` links, refs carry ``sections``.

    Local-only: a linked project, ``[plans].dir = ""`` and an empty index
    answer ``[]`` with no query beyond one probe. No ref carries ``objective``;
    ``plan.body`` is never selected.

    Parameters
    ----------
    shas : sequence of str
        The commit SHAs of the answer.
    paths : sequence of str
        The file path(s) of the answer.
    target : Target, optional
        The resolved target (symbol scope); with none, or with no anchor,
        only ``commit`` / ``pr`` links apply.
    scope : {"symbol", "file"}
        Which rules apply.
    detail : {"compact", "full"}
        ``full`` adds ``path``, ``present`` and ``commits`` (the input SHAs
        that are implementing commits of the plan).
    limit : int, optional
        The most refs; the scope's cap by default.

    Returns
    -------
    list of dict
        Ranked refs, possibly empty.
    """
    if _linked():
        return []
    plans_dir = get_config().plans.dir
    if not plans_dir:
        return []
    sha_list = list(dict.fromkeys(s for s in shas if s))
    path_list = list(dict.fromkeys(p for p in paths if p))
    symbol = scope == "symbol"
    cap = limit if limit is not None else (CARD_REFS_MAX if symbol else FILE_REFS_MAX)
    if cap <= 0:
        return []

    with get_session() as session:
        if session.exec(select(PlanLink.stem).limit(1)).first() is None:
            return []
        best: dict[str, str] = {}

        def offer(stem: str, kind: str) -> None:
            if stem not in best or LINK_KINDS.index(kind) < LINK_KINDS.index(
                best[stem]
            ):
                best[stem] = kind

        for chunk in _chunks(sha_list):
            for stem in session.exec(
                select(PlanLink.stem).where(
                    col(PlanLink.kind) == "commit", col(PlanLink.ref).in_(chunk)
                )
            ).all():
                offer(stem, "commit")
        if sha_list:
            by_number: dict[int, list[str]] = {}
            for stem, ref in session.exec(
                select(PlanLink.stem, PlanLink.ref).where(col(PlanLink.kind) == "pr")
            ).all():
                if ref.isdigit():
                    by_number.setdefault(int(ref), []).append(stem)
            wanted = set(sha_list)
            for chunk in _chunks(sorted(by_number)):
                for pr in session.exec(
                    select(
                        PullRequest.number,
                        PullRequest.merge_commit_sha,
                        PullRequest.head_sha,
                        PullRequest.commit_titles,
                    ).where(col(PullRequest.number).in_(chunk))
                ).all():
                    if (
                        pr.merge_commit_sha in wanted
                        or pr.head_sha in wanted
                        or wanted & _oids(pr.commit_titles)
                    ):
                        for stem in by_number[pr.number]:
                            offer(stem, "pr")

        file_links: dict[str, dict[str, Any]] = {}
        path_stems: set[str] = set()
        anchor_path = target.path if (symbol and target is not None) else None
        if symbol and anchor_path is not None:
            path_stems = set(
                session.exec(
                    select(PlanLink.stem).where(
                        col(PlanLink.kind) == "path", col(PlanLink.ref) == anchor_path
                    )
                ).all()
            )
        elif not symbol:
            for chunk in _chunks(path_list):
                for stem, _ref, sections, declared in session.exec(
                    select(
                        PlanLink.stem,
                        PlanLink.ref,
                        PlanLink.sections,
                        PlanLink.declared,
                    ).where(col(PlanLink.kind) == "path", col(PlanLink.ref).in_(chunk))
                ).all():
                    entry = file_links.setdefault(
                        stem, {"declared": False, "sections": set()}
                    )
                    entry["declared"] = entry["declared"] or bool(declared)
                    entry["sections"].update(_sections(sections))
                    offer(stem, "declared" if entry["declared"] else "file")

        candidates = sorted(set(best) | path_stems)
        if not candidates:
            return []
        rows: dict[str, Any] = {}
        for chunk in _chunks(candidates):
            for row in session.exec(
                select(
                    Plan.stem,
                    Plan.title,
                    Plan.path,
                    Plan.present,
                    Plan.updated_at,
                ).where(col(Plan.stem).in_(chunk))
            ).all():
                rows[row.stem] = row
        states = derive_status(session, list(rows), plans_dir)

        hints: dict[str, list[dict]] = {}
        if symbol and target is not None and path_stems & set(rows):
            symbols = _file_symbols(target.path)
            anchor = _resolve_anchor(target, symbols)
            if anchor is not None:
                whys: dict[str, dict] = {}
                for chunk in _chunks(sorted(path_stems & set(rows))):
                    for stem, raw in session.exec(
                        select(Plan.stem, Plan.why_index).where(
                            col(Plan.stem).in_(chunk)
                        )
                    ).all():
                        try:
                            parsed = json.loads(raw or "{}")
                        except ValueError:
                            parsed = {}
                        whys[stem] = parsed if isinstance(parsed, dict) else {}
                for stem, why in whys.items():
                    state = states[stem]
                    found, declares = _hints_for(
                        why, anchor, symbols, state.changed_paths
                    )
                    if found:
                        hints[stem] = found[:CARD_HINTS_MAX]
                    if declares and rows[stem].present and state.status == "shipped":
                        offer(stem, "declared")

    refs: list[tuple[Any, str, PlanState]] = []
    for stem, link in best.items():
        row = rows.get(stem)
        if row is None:
            continue
        if link in ("declared", "file") and not row.present:
            continue
        if symbol and link == "file":
            continue
        refs.append((row, link, states[stem]))

    refs.sort(key=lambda t: t[0].updated_at or "", reverse=True)
    if symbol:
        refs.sort(key=lambda t: (LINK_KINDS.index(t[1]), _STATUS_ORDER[t[2].status]))
    else:
        refs.sort(
            key=lambda t: (
                t[2].status == "not_implemented",
                LINK_KINDS.index(t[1]),
                _STATUS_ORDER[t[2].status],
            )
        )

    out: list[dict] = []
    file_count = 0
    for row, link, state in refs:
        if len(out) >= cap:
            break
        if not symbol and link == "file":
            if file_count >= FILE_LINKS_MAX:
                continue
            file_count += 1
        ref = _shape_ref(row, link, state.status, detail=detail)
        if symbol:
            if row.stem in hints:
                ref["hints"] = hints[row.stem]
        else:
            numbers = sorted(file_links.get(row.stem, {}).get("sections", ()))
            if numbers:
                ref["sections"] = numbers[:SECTIONS_MAX]
                if len(numbers) > SECTIONS_MAX:
                    ref["sections_total"] = len(numbers)
        if detail == "full":
            _finish_full(ref, row, sha_list, state.implementing_shas)
        out.append(ref)

    if symbol:
        while out and _serialized(out) > CARD_PLANS_CHARS:
            if out[-1].pop("hints", None) is None:
                out.pop()
    return out


# --- the two resources' bodies -------------------------------------------------------------


def _not_found(stem: str) -> WhyGraphError:
    return WhyGraphError(f"plan {stem!r} is not indexed", code="plan_not_found")


def _outline_of(raw: str) -> list[dict]:
    try:
        outline = json.loads(raw or "[]")
    except ValueError:
        return []
    return outline if isinstance(outline, list) else []


def _contents(outline: Sequence[dict]) -> tuple[list[dict], int]:
    """The level-2 entries as ``{n, title, chars, children}``, bounded (section 3.5).

    At most :data:`CONTENTS_MAX_ENTRIES` entries and :data:`CONTENTS_MAX_CHARS`
    serialized characters, dropped from the end. Returns the entries and
    the number of level-2 entries in all.
    """
    kids: dict[int, list[dict]] = {}
    for entry in outline:
        if entry.get("parent") is not None:
            kids.setdefault(entry["parent"], []).append(entry)
    top = [e for e in outline if e.get("level") == 2]
    items = [
        {
            "n": e["n"],
            "title": _clip(e["title"], REF_TITLE_MAX),
            "chars": (e["end"] - e["start"])
            + sum(k["end"] - k["start"] for k in kids.get(e["n"], [])),
            "children": len(kids.get(e["n"], [])),
        }
        for e in top
    ][:CONTENTS_MAX_ENTRIES]
    while items and len(json.dumps(items, ensure_ascii=False)) > CONTENTS_MAX_CHARS:
        items.pop()
    return items, len(top)


def plan_record(stem: str, *, full: bool = False, with_digest: bool = False) -> dict:
    """The digest form of one plan: what ``whygraph://plan/{stem}`` serves.

    ``full=True`` is for the Plans REST route only (nothing else may pass
    it, so no agent surface can return a whole body): it serves ``body``
    and ``truncated`` instead of ``digest`` and ``contents``, the larger
    link caps (10 commits, 10 PRs, 20 paths), no 8,500-character bound,
    and ``outline`` (``{n, level, title, part, step}``). ``with_digest``
    adds ``digest`` beside the body.

    ``kind``, ``reading``, the derived ``status``, ``objective``, the
    digest, the links (at most 5 implementing commits, 5 PRs and 10 paths,
    each with a total; subjects and titles clipped at 80) and ``contents``
    (the level-2 outline entries, at most 40 and 3,000 characters, with
    ``contents_total`` when cut). The whole answer stays within 8,500
    characters (lists are shortened from the end if it would not).
    Local-only: a linked project raises ``plan_not_found``.

    Parameters
    ----------
    stem : str
        The plan stem.
    full : bool
        REST form: the whole stored body (see above).
    with_digest : bool
        With ``full``, also return the digest.

    Returns
    -------
    dict
        The answer.

    Raises
    ------
    WhyGraphError
        ``plan_not_found`` for an invalid or unknown stem, or on a linked
        project.
    """
    if _linked() or not PLAN_STEM_RE.fullmatch(stem or ""):
        raise _not_found(stem)
    plans_dir = get_config().plans.dir
    commits_max, prs_max, paths_max = (
        (10, 10, 20) if full else (RECORD_COMMITS_MAX, RECORD_PRS_MAX, RECORD_PATHS_MAX)
    )
    with get_session() as session:
        row = session.exec(
            select(
                Plan.stem,
                Plan.title,
                Plan.path,
                Plan.present,
                Plan.objective,
                Plan.line_count,
                Plan.redacted_lines,
                Plan.digest,
                Plan.outline,
                Plan.body,
                Plan.truncated,
                Plan.updated_at,
            ).where(col(Plan.stem) == stem)
        ).first()
        if row is None:
            raise _not_found(stem)
        state = derive_status(session, [stem], plans_dir)[stem]
        all_paths = sorted(
            session.exec(
                select(PlanLink.ref).where(
                    col(PlanLink.stem) == stem, col(PlanLink.kind) == "path"
                )
            ).all()
        )
    contents, contents_total = _contents(_outline_of(row.outline))
    result: dict[str, Any] = {
        "kind": "plan",
        "reading": PLAN_READING,
        "stem": row.stem,
        "title": _clip(row.title or row.stem, REF_TITLE_MAX),
        "status": state.status,
        "uri": PLAN_URI.format(stem=row.stem),
        "path": row.path,
        "present": bool(row.present),
        "objective": row.objective,
        "line_count": row.line_count,
        "redacted_lines": row.redacted_lines,
        "publishing_links": state.publishing,
        "implementing_commits": [
            {
                "sha": c["sha"][:8],
                "subject": _clip(c["subject"] or "", REF_TITLE_MAX),
                "on_default_branch": c["on_default_branch"],
            }
            for c in state.implementing[:commits_max]
        ],
        "implementing_commits_total": len(state.implementing),
        "pull_requests": [
            {
                "number": p["number"],
                "title": _clip(p["title"] or "", REF_TITLE_MAX),
                "state": p["state"],
            }
            for p in state.prs[:prs_max]
        ],
        "pull_requests_total": len(state.prs),
        "paths": all_paths[:paths_max],
        "paths_total": len(all_paths),
    }
    if full:
        result["updated_at"] = row.updated_at
        result["body"] = row.body
        result["truncated"] = bool(row.truncated)
        if with_digest:
            result["digest"] = row.digest
        result["outline"] = [
            {
                "n": e.get("n"),
                "level": e.get("level"),
                "title": e.get("title"),
                "part": e.get("part"),
                "step": int(m.group(1))
                if (m := re.match(r"Step (\d+):", e.get("title") or ""))
                else None,
            }
            for e in _outline_of(row.outline)
        ]
        return result
    result["digest"] = row.digest
    result["contents"] = contents
    if contents_total > len(contents):
        result["contents_total"] = contents_total
    # The whole answer is bounded (criterion 15h): shorten the lists, not the digest.
    for key in ("paths", "pull_requests", "implementing_commits"):
        while (
            len(json.dumps(result, ensure_ascii=False)) > RECORD_MAX_CHARS
            and result[key]
        ):
            result[key].pop()
    return result


def plans_search(
    query: str | None,
    status: str | None,
    *,
    limit: int = 10,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Search the indexed plans for the Plans page (people only).

    Matches ``query`` (escaped) against title, objective and stem, derives
    each row's status, filters by ``status`` before slicing, and sorts
    present rows first, then newest ``updated_at``. Reads at most 500 rows
    (the index cap). Local-only: a linked project answers ``([], 0)``.

    Parameters
    ----------
    query : str or None
        Free text, or ``None`` for every plan.
    status : str or None
        ``shipped``, ``in_flight``, ``not_implemented`` or ``None``.
    limit : int
        Page size.
    offset : int
        Rows to skip.

    Returns
    -------
    tuple of (list of dict, int)
        The page of refs (no ``link``) and the filtered total.
    """
    if _linked():
        return [], 0
    plans_dir = get_config().plans.dir
    stmt = select(
        Plan.stem,
        Plan.title,
        Plan.path,
        Plan.present,
        Plan.objective,
        Plan.updated_at,
    )
    text = (query or "").strip()
    if text:
        pattern = f"%{like_escape(text)}%"
        stmt = stmt.where(
            or_(
                col(Plan.title).like(pattern, escape=LIKE_ESCAPE_CHAR),
                col(Plan.objective).like(pattern, escape=LIKE_ESCAPE_CHAR),
                col(Plan.stem).like(pattern, escape=LIKE_ESCAPE_CHAR),
            )
        )
    with get_session() as session:
        rows = session.exec(stmt.limit(500)).all()
        states = derive_status(session, [r.stem for r in rows], plans_dir)
    items = []
    for r in rows:
        state = states[r.stem].status
        if status and state != status:
            continue
        items.append(
            {
                "stem": r.stem,
                "title": _clip(r.title or r.stem, REF_TITLE_MAX),
                "status": state,
                "uri": PLAN_URI.format(stem=r.stem),
                "path": r.path,
                "present": bool(r.present),
                "objective": r.objective,
                "updated_at": r.updated_at,
            }
        )
    items.sort(key=lambda i: i["updated_at"] or "", reverse=True)
    items.sort(key=lambda i: not i["present"])
    return items[offset : offset + limit], len(items)


def _parse_n(n: Any) -> int | None:
    """``n`` as a section number, or ``None`` when it is not a plain integer."""
    if isinstance(n, bool):
        return None
    if isinstance(n, int):
        return n
    if isinstance(n, str) and n.isascii() and n.isdigit() and len(n) <= 9:
        return int(n)
    return None


def plan_section(stem: str, n: Any) -> dict:
    """One outline entry's own text: what ``whygraph://plan/{stem}/section/{n}`` serves.

    Sliced from the stored body by the stored offsets. Carries ``kind``,
    ``reading``, the derived ``status``, ``n``, ``title``, ``level``,
    ``text`` (at most 8,000 characters, never shortened), ``chars``,
    ``children`` (``[{n, title, chars}]``, at most 20, ``children_total``
    when more), ``prev`` / ``next``, ``version`` (the first 8 characters of
    the blob SHA) and, when the entry holds echo, ``copies`` and
    ``copies_note``. Entry 0 is served with the plan's own ``**Status:**``
    line replaced. Local-only.

    Parameters
    ----------
    stem : str
        The plan stem.
    n : int
        The outline number; a non-integer is a miss.

    Returns
    -------
    dict
        The answer.

    Raises
    ------
    WhyGraphError
        ``plan_not_found`` for an unknown stem or section.
    """
    number = _parse_n(n)
    if _linked() or number is None or not PLAN_STEM_RE.fullmatch(stem or ""):
        raise _not_found(stem)
    plans_dir = get_config().plans.dir
    with get_session() as session:
        row = session.exec(
            select(Plan.stem, Plan.blob_sha, Plan.outline, Plan.body).where(
                col(Plan.stem) == stem
            )
        ).first()
        if row is None:
            raise _not_found(stem)
        outline = _outline_of(row.outline)
        if not 0 <= number < len(outline):
            raise _not_found(stem)
        state = derive_status(session, [stem], plans_dir)[stem]
    entry = outline[number]
    own = row.body[entry["start"] : entry["end"]]
    text = own
    if number == 0:
        text = _STATUS_LINE_RE.sub(lambda _m: STATUS_LINE_REPLACEMENT, text)
    if entry.get("cut"):
        text += LINE_CUT_NOTICE
    children = [e for e in outline if e.get("parent") == number]
    result: dict[str, Any] = {
        "kind": "plan",
        "reading": PLAN_READING,
        "stem": stem,
        "status": state.status,
        "n": number,
        "title": entry["title"],
        "level": entry["level"],
        "text": text,
        "chars": len(own),
        "children": [
            {
                "n": c["n"],
                "title": _clip(c["title"], REF_TITLE_MAX),
                "chars": c["end"] - c["start"],
            }
            for c in children[:CHILDREN_MAX]
        ],
        "prev": number - 1 if number > 0 else None,
        "next": number + 1 if number + 1 < len(outline) else None,
        "version": (row.blob_sha or "")[:8],
    }
    if len(children) > CHILDREN_MAX:
        result["children_total"] = len(children)
    if entry.get("echo"):
        result["copies"] = True
        result["copies_note"] = COPIES_NOTE
    return result


# --- MCP resources --------------------------------------------------------------------------

_DB_UNSCANNED_MESSAGE = (
    "WhyGraph DB is missing or unscanned - scan from the WhyGraph portal first"
)


def _read_resource(read: Callable[[], dict], missing: dict) -> dict:
    """Run ``read``; a ``plan_not_found`` is content (``resources.py`` rule 2)."""
    try:
        return read()
    except WhyGraphError as exc:
        if exc.code == "plan_not_found":
            return missing
        raise
    except OperationalError as exc:
        raise WhyGraphError(_DB_UNSCANNED_MESSAGE) from exc


def _remote() -> "RemoteProject | None":
    """The bound project's platform, or ``None`` for an ordinary project."""
    ctx = current_project()
    return None if ctx is None else ctx.remote


def _plan_resource(stem: str) -> dict:
    """Read the resource backing ``whygraph://plan/{stem}``.

    A linked project reads the plan from its platform (``remote.plan``,
    which checks the stem against the stem pattern before any request).
    """
    _log.debug("plan resource read: stem=%r", stem)
    missing = {"error": "not_found", "stem": stem}
    remote = _remote()
    if remote is not None:
        from .resources import _linked_read

        return _linked_read(remote, lambda: remote.plan(stem), missing)
    return _read_resource(lambda: plan_record(stem), missing)


def _plan_section_resource(stem: str, n: str) -> dict:
    """Read the resource backing ``whygraph://plan/{stem}/section/{n}``.

    On a linked project a non-integer ``n`` is a miss with no request;
    ``remote.plan_section`` checks the stem and the range before one.
    """
    _log.debug("plan section resource read: stem=%r n=%r", stem, n)
    missing = {"error": "not_found", "stem": stem, "section": n}
    remote = _remote()
    if remote is not None:
        from whygraph.core.remote import platform_block

        from .resources import _linked_read

        number = _parse_n(n)
        if number is None:
            return {**missing, "platform": platform_block(remote)}
        return _linked_read(remote, lambda: remote.plan_section(stem, number), missing)
    return _read_resource(lambda: plan_section(stem, n), missing)


def attach_linked_plans(
    result: dict,
    remote: "RemoteProject",
    shas: Sequence[str],
    paths: Sequence[str],
    *,
    scope: Literal["symbol", "file"],
    target: "Target | None" = None,
    detail: Literal["compact", "full"] = "compact",
) -> None:
    """Add a linked tool answer's ``plans`` from its platform (plan 61 section 4.8).

    The two linked tool branches (``linked_area_history``,
    ``linked_rationale``) are the only callers, and they pass only the
    SHAs, path and target their own call already sent. At most
    :data:`~whygraph.api_v1.MAX_LIMIT` distinct SHAs go, in the given order.
    An older platform sets ``result["platform"]["plans"] = "unsupported"``;
    any failure (an unreadable ``meta``, an unreachable platform) leaves
    ``result`` without ``plans`` - it never fails the tool.

    Parameters
    ----------
    result : dict
        The tool answer, with its ``platform`` block already set.
    remote : RemoteProject
        The project's platform.
    shas, paths : Sequence[str]
        What the same call already sent.
    scope : {"symbol", "file"}
        Beside a card, or a file-level answer.
    target : Target or None, optional
        The target the same call sent (symbol scope only).
    detail : {"compact", "full"}, optional
        The ref form.
    """
    from whygraph.api_v1 import MAX_LIMIT

    try:
        refs, supported = remote.plans_for(
            list(dict.fromkeys(shas))[:MAX_LIMIT],
            list(paths),
            scope=scope,
            target=target,
            detail=detail,
        )
    except Exception:
        _log.warning("linked plans lookup failed", exc_info=True)
        return
    if not supported:
        result.setdefault("platform", {})["plans"] = "unsupported"
    elif refs:
        result["plans"] = refs


def register(mcp: FastMCP) -> None:
    """Attach the two plan resources to an MCP server (no full-text resource)."""
    mcp.resource(
        PLAN_URI,
        name="whygraph_plan",
        description=(
            "A committed plan's decision digest, its links and its contents "
            "list. Start here."
        ),
        mime_type="application/json",
    )(_plan_resource)
    mcp.resource(
        PLAN_SECTION_URI,
        name="whygraph_plan_section",
        description=(
            "One section of a plan, at most 8,000 characters. Use the `n` of a "
            "ref's `hints` or `sections`, or of the plan's contents."
        ),
        mime_type="application/json",
    )(_plan_section_resource)
