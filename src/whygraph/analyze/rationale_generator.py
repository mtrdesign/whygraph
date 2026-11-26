"""LLM-driven rationale for a piece of code's change history.

This is the raw service that turns an evidence bundle — scanned commits
with their linked pull requests and issues, optionally enriched with the
target symbol's code-graph context — into a structured
:class:`~whygraph.analyze.Rationale` card explaining *why* the code exists.
It owns only the prompt + LLM round-trip:

* No git or database access — the evidence is an input.
* No persistence — the :class:`~whygraph.analyze.Rationale` is an output.
* No chunking — one bundle in, one card out, in a single LLM call.

Gathering the evidence (git blame, database lookups, grouping pull requests
and issues onto commits) is intentionally out of scope so this class stays
trivially testable: feed a stub :class:`LlmClient` and a list of
:class:`~whygraph.analyze.CommitEvidence`, assert on the returned
:class:`~whygraph.analyze.Rationale`.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from whygraph.core.code_anchor import anchor_positions
from whygraph.core.config import Config, RationaleConfig
from whygraph.core.usage import record_usage
from whygraph.db.models import Commit, Issue, PullRequest
from whygraph.services.codegraph import Relation, SymbolContext
from whygraph.services.llm import (
    CompletionRequest,
    LlmClient,
    LlmClientFactory,
    LlmError,
)

from .exceptions import AnalyzeError, RationaleError
from .prompt import RATIONALE_PLACEHOLDER, Prompt, render, resolve
from .rationale import CommitEvidence, Item, ItemRef, Rationale

# Prompt component tag — the subtree under ``analyze/prompts/`` that holds
# this class's ``system`` / ``task`` markdown files.
_PROMPT_COMPONENT = "rationale_generator"

# The rationale JSON schema, split by the validation each key needs.
_REQUIRED_STR_FIELDS = ("purpose", "why")
_REQUIRED_LIST_FIELDS = ("constraints", "tradeoffs", "risks")
# The list keys whose items are ``{"text", "refs", "quote"}`` objects (or a
# plain string); ``tradeoffs`` stays a list of strings.
_SOURCED_LIST_FIELDS = ("constraints", "risks")

# A ref the model names: "PR #12" / "Issue #7", or a commit SHA prefix.
_NUMBERED_REF_RE = re.compile(r"(PR|Issue)\s*#\s*(\d+)", re.IGNORECASE)
_SHA_REF_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_WHITESPACE_RE = re.compile(r"\s+")


def _short_sha(sha: str) -> str:
    """Return the first eight characters of a commit SHA."""
    return sha[:8]


def _labels_suffix(raw: str) -> str:
    """Render a JSON-encoded label list as a `` [a, b]`` suffix.

    ``PullRequest.labels`` and ``Issue.labels`` are stored as JSON-encoded
    strings. A malformed or empty list yields an empty string so the bundle
    line stays clean.
    """
    try:
        labels = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(labels, list) or not labels:
        return ""
    return "  [" + ", ".join(str(label) for label in labels) + "]"


def _json_list(raw: str | None) -> list:
    """Decode a JSON-encoded list column; empty list on anything malformed.

    Mirrors :func:`whygraph.mcp.evidence._json_list` — kept local so the
    formatters do not depend on the MCP layer.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass(frozen=True, slots=True)
class _PrRenderCaps:
    """Size caps for rendering a PR block into the rationale prompt.

    Passed explicitly into the module-level formatters (rather than read
    from a global :func:`get_config`) so they stay pure and unit-testable.
    Defaults mirror :class:`~whygraph.core.config.RationaleConfig`.

    Attributes
    ----------
    pr_roster_max_commits : int
        Max squashed-commit headlines rendered into a PR block.
    pr_discussion_max_comments : int
        Max PR comments rendered into a PR block.
    pr_comment_max_chars : int
        Per-comment body clip.
    review_threads_max_chars : int
        Characters of qualified review-thread text in one prompt; ``0``
        keeps threads out.
    """

    pr_roster_max_commits: int = 30
    pr_discussion_max_comments: int = 20
    pr_comment_max_chars: int = 500
    review_threads_max_chars: int = 3000

    @classmethod
    def from_config(cls, config: RationaleConfig) -> "_PrRenderCaps":
        """Project the four rendering caps out of a :class:`RationaleConfig`."""
        return cls(
            pr_roster_max_commits=config.pr_roster_max_commits,
            pr_discussion_max_comments=config.pr_discussion_max_comments,
            pr_comment_max_chars=config.pr_comment_max_chars,
            review_threads_max_chars=config.review_threads_max_chars,
        )


def _indent_block(text: str, prefix: str) -> str:
    """Indent every line of ``text`` by ``prefix``."""
    return "\n".join(prefix + line for line in text.splitlines())


def _format_commit(commit: Commit) -> list[str]:
    """Render one commit as the lines of an evidence block.

    The ``llm_description`` diff summary and a blank ``body`` are omitted so
    the bundle only carries narratives that exist.
    """
    lines = [
        f"COMMIT {_short_sha(commit.sha)}  {commit.committed_at}  "
        f"by {commit.author_name}"
    ]
    if commit.llm_description:
        lines.append(f"  Summary: {commit.llm_description}")
    lines.append(f"  Subject: {commit.subject}")
    if commit.body.strip():
        lines.append("  Body:")
        lines.append(_indent_block(commit.body.strip(), "    "))
    return lines


def _format_pr(
    pr: PullRequest,
    caps: _PrRenderCaps = _PrRenderCaps(),
    *,
    discussion: bool = True,
) -> list[str]:
    """Render one pull request as the indented lines of an evidence block.

    Appends the squashed-commit roster and the PR discussion so the LLM
    sees the narrative a squash merge collapsed. Both are clipped by
    ``caps`` to bound the prompt size; ``pr.commit_titles`` / ``pr.comments``
    are JSON-encoded list columns decoded via :func:`_json_list`.
    ``discussion=False`` leaves the ``Discussion:`` block out (a later
    occurrence of a PR already rendered in the bundle).
    """
    when = f"merged {pr.merged_at}" if pr.merged_at else pr.state
    author = f"by {pr.author}" if pr.author else "by unknown"
    lines = [f"  PR #{pr.number}  {author}  {when}{_labels_suffix(pr.labels)}"]
    lines.append(f"    Title: {pr.title}")
    if pr.body and pr.body.strip():
        lines.append("    Body:")
        lines.append(_indent_block(pr.body.strip(), "      "))
    titles = _json_list(pr.commit_titles)[: caps.pr_roster_max_commits]
    if titles:
        lines.append("    Squashed commits:")
        for c in titles:
            if not isinstance(c, dict):
                continue
            oid = (c.get("oid") or "")[:9]
            lines.append(f"      - {c.get('headline', '')}  ({oid})")
    comments = _json_list(pr.comments)[: caps.pr_discussion_max_comments]
    if comments and discussion:
        lines.append("    Discussion:")
        for cm in comments:
            if not isinstance(cm, dict):
                continue
            who = cm.get("author") or "unknown"
            body = (cm.get("body") or "").strip()[: caps.pr_comment_max_chars]
            lines.append(_indent_block(f"[{who}] {body}", "      "))
    return lines


def _format_issue(issue: Issue) -> list[str]:
    """Render one issue as the indented lines of an evidence block."""
    author = f"by {issue.author}" if issue.author else "by unknown"
    lines = [
        f"  Issue #{issue.number}  {issue.state}  "
        f"{author}{_labels_suffix(issue.labels)}"
    ]
    lines.append(f"    Title: {issue.title}")
    if issue.body and issue.body.strip():
        lines.append("    Body:")
        lines.append(_indent_block(issue.body.strip(), "      "))
    return lines


_SOURCE_LABELS = {
    "blame": "line-blame",
    "pr-origin": "original commit recovered from a squash-merged PR",
    "blame-walked": "line-blame (skipped a refactor commit)",
    "predecessor-blame": "line-blame on a pre-rename predecessor file",
    "area": "area-history (touched the file but not these lines)",
}

# Evidence sources that attribute the target's own lines (plan 62 R20): only
# a PR linked to one of these may bring review threads into the prompt.
_LINE_SOURCES = frozenset({"blame", "pr-origin", "blame-walked", "predecessor-blame"})

# Review-thread rendering (plan 62 section 4.9 / 4.9a).
_THREADS_HEADER = (
    "    Review threads on these lines (comments on a draft of this change):"
)
_THREADS_PER_PR_CHARS = 1200
_THREAD_COMMENT_MAX_CHARS = 400
_THREAD_AUTHOR_MIN_CHARS = 60
_THREAD_OUTDATED = "(code changed after this comment)"
_DISCUSSION_SHOWN_ABOVE = "    (discussion shown above)"

# Agent or bot text posted from a ``User`` account: an HTML-comment marker
# (``<!-- cookie-review``), an agent signature, or an agent severity label.
# Matched on the raw comment body.
_MACHINE_TEXT_RE = re.compile(
    r"^\s*<!--"
    r"|written by .{0,40}agent"
    r"|agent-assisted"
    r"|^\s*(\*\*|\[)\s*(p[0-3]|critical|high|medium|low|optional)\b",
    re.IGNORECASE,
)
_SUGGESTION_BLOCK_RE = re.compile(r"```[ \t]*suggestion\b.*?(?:```|\Z)", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)


def _clean_thread_comment(body: object) -> str:
    """The text of one review-thread comment as the prompt may show it.

    Removes suggestion blocks and HTML comments, replaces any other fenced
    code block with ``[code]``, drops lines that start with ``>`` (text the
    commenter quoted) and collapses whitespace. The one cleaning used for
    the length rule, the rendering and the quote sources alike; ``""``
    means the comment is dropped.
    """
    if not isinstance(body, str):
        return ""
    text = _SUGGESTION_BLOCK_RE.sub("", body)
    text = _HTML_COMMENT_RE.sub("", text)
    text = _CODE_FENCE_RE.sub("[code]", text)
    kept = [line for line in text.split("\n") if not line.lstrip().startswith(">")]
    return _collapse_whitespace("\n".join(kept))


def _is_machine_text(body: object) -> bool:
    """Whether a raw comment body is agent or bot text (:data:`_MACHINE_TEXT_RE`)."""
    return isinstance(body, str) and _MACHINE_TEXT_RE.search(body) is not None


def _parse_time(value: object) -> datetime | None:
    """An ISO-8601 timestamp as an aware datetime (naive read as UTC), else ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _RenderedThreads:
    """One PR's review-thread block, as the prompt shows it.

    Attributes
    ----------
    lines : tuple[str, ...]
        The block's indented lines (header, threads, the omitted line).
    author_texts : tuple[str, ...]
        The PR author's rendered, cleaned comments: the only thread texts a
        quote may come from (plan 62 R14).
    """

    lines: tuple[str, ...]
    author_texts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Thread:
    """One qualified thread: its rendered entries and the PR author's texts."""

    entries: tuple[str, ...]
    author_texts: tuple[str, ...]

    @property
    def chars(self) -> int:
        """The thread text this thread spends from the budgets."""
        return sum(len(entry) for entry in self.entries)


def _covers(ranges: Sequence[tuple[int, int]] | None, line: int) -> bool:
    """Whether one of the inclusive ``ranges`` holds ``line``."""
    return any(start <= line <= end for start, end in ranges or ())


def _anchor_owned(
    thread: dict,
    pr: PullRequest,
    blame: Sequence[CommitEvidence],
    file_lines: Sequence[str],
    target_range: tuple[int, int],
) -> bool:
    """Rule 4 (R21, R22): the thread's code is inside the target and still the PR's.

    The anchor's commented line must lie inside ``target_range`` at a
    position that blame ties to the PR: for an ``added`` line, a ``blame``
    item covering it is linked to this PR; for a context line, the anchor
    occurs once in the whole file and the ``blame`` item covering it was
    committed before the PR was created.
    """
    anchor = thread.get("anchor")
    if not isinstance(anchor, dict):
        return False
    anchor_lines = anchor.get("lines")
    if not isinstance(anchor_lines, list) or not all(
        isinstance(line, str) for line in anchor_lines
    ):
        return False
    positions = anchor_positions(anchor_lines, file_lines)
    start, end = target_range
    added = anchor.get("added") is True
    created = _parse_time(pr.created_at)
    if not added and (len(positions) != 1 or created is None):
        return False
    for y in positions:
        if not start <= y <= end:
            continue
        for item in blame:
            if not _covers(item.lines, y):
                continue
            if added:
                if any(p.number == pr.number for p in item.pull_requests):
                    return True
            else:
                committed = _parse_time(item.commit.committed_at)
                if committed and created and committed < created:
                    return True
    return False


def _qualify_thread(
    thread: object,
    pr: PullRequest,
    paths: frozenset[str],
    blame: Sequence[CommitEvidence],
    file_lines: Sequence[str],
    target_range: tuple[int, int],
) -> _Thread | None:
    """One stored thread as the prompt renders it, or ``None`` when it does not qualify.

    Applies plan 62 section 4.9a rules 1, 2 and 4 (rule 3 is per PR, in
    :func:`_select_threads`) and builds the opener plus the last two
    remaining comments, each cleaned and clipped.
    """
    if not isinstance(thread, dict) or thread.get("path") not in paths:
        return None
    comments = [c for c in thread.get("comments") or () if isinstance(c, dict)]
    if not comments:
        return None
    opener = comments[0]
    # Rule 1: a flagged or machine-written opener is context for agents only.
    if opener.get("bot") or opener.get("minimized"):
        return None
    if _is_machine_text(opener.get("body")):
        return None
    kept: list[tuple[str, str]] = []
    for comment in comments:
        if comment.get("bot") or comment.get("minimized"):
            continue
        if _is_machine_text(comment.get("body")):
            continue
        text = _clean_thread_comment(comment.get("body"))
        if text:
            kept.append((str(comment.get("author") or "unknown"), text))
    rendered = kept[:1] + kept[1:][-2:]
    pr_author = (pr.author or "").lower()
    # Rule 2: the PR author explains the change in a rendered comment.
    if not pr_author or not any(
        who.lower() == pr_author and len(text) >= _THREAD_AUTHOR_MIN_CHARS
        for who, text in rendered
    ):
        return None
    if not _anchor_owned(thread, pr, blame, file_lines, target_range):
        return None
    entries: list[str] = []
    author_texts: list[str] = []
    for who, text in rendered:
        shown = text[:_THREAD_COMMENT_MAX_CHARS]
        is_author = who.lower() == pr_author
        role = "PR author" if is_author else "reviewer"
        entries.append(f"[{who}, {role}] {shown}")
        if is_author:
            author_texts.append(shown)
    if thread.get("outdated") is True:
        entries.append(_THREAD_OUTDATED)
    return _Thread(tuple(entries), tuple(author_texts))


def _thread_lines(thread: _Thread) -> list[str]:
    """A rendered thread: the first entry as a bullet, the rest beneath it."""
    first, *rest = thread.entries
    return [f"      - {first}", *(f"        {entry}" for entry in rest)]


def _select_threads(
    evidence: Sequence[CommitEvidence],
    paths: frozenset[str] | None,
    file_lines: Sequence[str] | None,
    target_range: tuple[int, int] | None,
    caps: _PrRenderCaps,
) -> dict[int, _RenderedThreads]:
    """The review threads a bundle's prompt shows, per PR number (plan 62 section 4.9a).

    The one selection that both :func:`_format_evidence` and
    :func:`_evidence_sources` use, so what is rendered and what may be
    quoted cannot drift. A thread qualifies only when its opener is human
    text, the PR author wrote a substantive rendered comment, its PR is
    linked to a line-level evidence item, and its anchor lies inside the
    target's lines at a position blame ties to the PR. Qualified threads
    fill :data:`_THREADS_PER_PR_CHARS` per PR and
    ``caps.review_threads_max_chars`` per card in bundle order, stopping at
    the first thread that does not fit.

    Parameters
    ----------
    evidence : Sequence[CommitEvidence]
        The bundle, in prompt order.
    paths : frozenset[str] or None
        The target file and its rename aliases.
    file_lines : Sequence[str] or None
        The target file's current lines (the whole file).
    target_range : tuple[int, int] or None
        The target's inclusive line range in ``file_lines``.
    caps : _PrRenderCaps
        The rendering caps.

    Returns
    -------
    dict[int, _RenderedThreads]
        One entry per PR with at least one qualified thread; empty when any
        of ``paths`` / ``file_lines`` / ``target_range`` is ``None`` (fail
        closed) or the card budget is ``0``.
    """
    if (
        paths is None
        or file_lines is None
        or target_range is None
        or caps.review_threads_max_chars <= 0
    ):
        return {}
    line_linked = {
        pr.number
        for item in evidence
        if item.source in _LINE_SOURCES
        for pr in item.pull_requests
    }
    blame = [item for item in evidence if item.source == "blame" and item.lines]
    prs: dict[int, PullRequest] = {}
    for item in evidence:
        for pr in item.pull_requests:
            prs.setdefault(pr.number, pr)
    card_left = caps.review_threads_max_chars
    card_full = False
    selected: dict[int, _RenderedThreads] = {}
    for number, pr in prs.items():
        if number not in line_linked or not pr.author:
            continue
        qualified = [
            thread
            for raw in _json_list(pr.review_threads)
            if (
                thread := _qualify_thread(
                    raw, pr, paths, blame, file_lines, target_range
                )
            )
            is not None
        ]
        if not qualified:
            continue
        pr_left = _THREADS_PER_PR_CHARS
        shown: list[_Thread] = []
        for thread in qualified:
            if card_full or thread.chars > card_left:
                # The card budget stops every later thread, in any PR.
                card_full = True
                break
            if thread.chars > pr_left:
                break
            shown.append(thread)
            pr_left -= thread.chars
            card_left -= thread.chars
        lines = [_THREADS_HEADER]
        for thread in shown:
            lines.extend(_thread_lines(thread))
        omitted = len(qualified) - len(shown)
        if omitted:
            lines.append(f"    ({omitted} more review threads omitted)")
        selected[number] = _RenderedThreads(
            tuple(lines),
            tuple(text for thread in shown for text in thread.author_texts),
        )
    return selected


def _has_discussion(pr: PullRequest, caps: _PrRenderCaps) -> bool:
    """Whether :func:`_format_pr` renders a ``Discussion:`` block for ``pr``."""
    return bool(_json_list(pr.comments)[: caps.pr_discussion_max_comments])


def _format_evidence(
    evidence: Sequence[CommitEvidence],
    caps: _PrRenderCaps = _PrRenderCaps(),
    threads: dict[int, _RenderedThreads] | None = None,
) -> str:
    """Render an evidence bundle as the text payload for the rationale prompt.

    Commits are formatted in the order given — the caller controls
    ordering. Each commit block is followed by its linked pull requests
    and issues. The JSON-encoded ``labels`` columns are decoded here.

    Each commit block also carries a ``Source:`` line describing *how*
    the row reached this bundle (line-blame, area-history, etc.), so the
    LLM can weight precision-vs-coverage signals when synthesising the
    rationale.

    ``caps`` bounds the per-PR roster / discussion rendering (see
    :func:`_format_pr`). ``threads`` (:func:`_select_threads`) adds a PR's
    review-thread block after its discussion. A PR's discussion and threads
    render once per bundle: later occurrences of the PR keep the rest of
    the block and say ``(discussion shown above)`` instead, when the first
    showed any.
    """
    threads = threads or {}
    n_prs = sum(len(item.pull_requests) for item in evidence)
    n_issues = sum(len(item.issues) for item in evidence)
    blocks = [
        f"Evidence: {len(evidence)} commit(s), {n_prs} PR(s), {n_issues} issue(s)."
    ]
    # PR number -> whether its first occurrence showed a discussion or threads.
    shown: dict[int, bool] = {}
    for item in evidence:
        lines = _format_commit(item.commit)
        label = _SOURCE_LABELS.get(item.source, item.source)
        # Insert the source line right after the COMMIT header so the
        # LLM sees it before subject/body content.
        lines.insert(1, f"  Source: {label}")
        for pr in item.pull_requests:
            lines.append("")
            if pr.number in shown:
                lines.extend(_format_pr(pr, caps, discussion=False))
                if shown[pr.number]:
                    lines.append(_DISCUSSION_SHOWN_ABOVE)
                continue
            lines.extend(_format_pr(pr, caps))
            block = threads.get(pr.number)
            if block is not None:
                lines.extend(block.lines)
            shown[pr.number] = _has_discussion(pr, caps) or block is not None
        for issue in item.issues:
            lines.append("")
            lines.extend(_format_issue(issue))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _collapse_whitespace(text: str) -> str:
    """Make each run of whitespace one space and strip the ends."""
    return _WHITESPACE_RE.sub(" ", text).strip()


@dataclass(frozen=True, slots=True)
class _Source:
    """One piece of evidence a card item may cite, with the texts a quote may come from.

    ``texts`` are whitespace-collapsed, one per field (title, body, a
    comment), so a quote must sit inside one of them.
    """

    ref: ItemRef
    texts: tuple[str, ...]


def _pr_texts(pr: PullRequest, caps: _PrRenderCaps) -> list[str]:
    """The human-written texts of a PR, clipped as :func:`_format_pr` renders them."""
    texts = [pr.title, pr.body or ""]
    for c in _json_list(pr.commit_titles)[: caps.pr_roster_max_commits]:
        if isinstance(c, dict):
            texts.append(str(c.get("headline", "")))
    for cm in _json_list(pr.comments)[: caps.pr_discussion_max_comments]:
        if isinstance(cm, dict):
            texts.append((cm.get("body") or "").strip()[: caps.pr_comment_max_chars])
    return texts


def _source(ref: ItemRef, texts: Sequence[str]) -> _Source:
    """A :class:`_Source` over the non-empty, whitespace-collapsed ``texts``."""
    collapsed = (_collapse_whitespace(t) for t in texts)
    return _Source(ref, tuple(t for t in collapsed if t))


def _evidence_sources(
    evidence: Sequence[CommitEvidence],
    caps: _PrRenderCaps = _PrRenderCaps(),
    threads: dict[int, _RenderedThreads] | None = None,
) -> dict[tuple[str, str], _Source]:
    """Index the evidence a card item may cite, by ``(kind, id)``.

    Built beside :func:`_format_evidence` and kept in memory only. A commit's
    texts are its subject and body, not its ``llm_description``: the diff
    summary is model output, so a quote from it proves nothing. A PR's texts
    are its title, body, squashed-commit headlines and discussion, plus the
    PR author's rendered review-thread comments from ``threads`` (the same
    :func:`_select_threads` result the prompt renders; reviewer comments are
    never a source); an issue's its title and body.
    """
    threads = threads or {}
    sources: dict[tuple[str, str], _Source] = {}
    for item in evidence:
        commit = item.commit
        key = ("commit", commit.sha)
        if key not in sources:
            sources[key] = _source(
                ItemRef("commit", commit.sha), (commit.subject, commit.body)
            )
        for pr in item.pull_requests:
            key = ("pr", str(pr.number))
            if key not in sources:
                ref = ItemRef("pr", str(pr.number), pr.html_url or None)
                block = threads.get(pr.number)
                author_texts = block.author_texts if block is not None else ()
                sources[key] = _source(ref, [*_pr_texts(pr, caps), *author_texts])
        for issue in item.issues:
            key = ("issue", str(issue.number))
            if key not in sources:
                ref = ItemRef("issue", str(issue.number), issue.html_url or None)
                sources[key] = _source(ref, (issue.title, issue.body or ""))
    return sources


def _resolve_ref(
    raw: object, sources: dict[tuple[str, str], _Source]
) -> _Source | None:
    """The source a model-written ref names, or ``None`` when it is not in the bundle.

    ``"PR #12"`` and ``"Issue #12"`` are different sources. A commit ref is
    a SHA prefix of at least 7 hex characters that matches exactly one
    commit of the bundle (the bundle shows 8).
    """
    if not isinstance(raw, str):
        return None
    ref = raw.strip()
    numbered = _NUMBERED_REF_RE.fullmatch(ref)
    if numbered:
        kind = "pr" if numbered.group(1).lower() == "pr" else "issue"
        return sources.get((kind, str(int(numbered.group(2)))))
    if _SHA_REF_RE.fullmatch(ref):
        prefix = ref.lower()
        hits = [
            source
            for (kind, sha), source in sources.items()
            if kind == "commit" and sha.lower().startswith(prefix)
        ]
        return hits[0] if len(hits) == 1 else None
    return None


def _item(raw: str | dict, sources: dict[tuple[str, str], _Source]) -> Item:
    """Turn one validated constraint or risk of the reply into an :class:`Item`.

    The basis is decided here, never taken from the model (plan 54 D6):

    * ``"quoted"`` - a ref is in the bundle and the quote, whitespace runs
      collapsed, is inside one text of one of those refs;
    * ``"cited"`` - a ref is in the bundle, but the quote is empty or not
      found;
    * ``"inferred"`` - no ref is in the bundle, the item is a plain string,
      or ``refs`` / ``quote`` is missing or of the wrong type.

    A ref that is not in the bundle is dropped. Bad refs or a bad quote never
    raise.

    Parameters
    ----------
    raw : str or dict
        A plain string, or an object whose ``text`` is a string (checked by
        :func:`_parse_rationale_json`).
    sources : dict
        The bundle's sources (:func:`_evidence_sources`).

    Returns
    -------
    Item
        The item; its ``refs`` are empty and ``quote`` is ``""`` unless the
        basis says otherwise.
    """
    if isinstance(raw, str):
        return Item(raw, "inferred")
    text = raw["text"]
    refs, quote = raw.get("refs"), raw.get("quote")
    if not isinstance(refs, list) or not isinstance(quote, str):
        return Item(text, "inferred")
    found: list[_Source] = []
    for ref in refs:
        source = _resolve_ref(ref, sources)
        if source is not None and source not in found:
            found.append(source)
    if not found:
        return Item(text, "inferred")
    item_refs = tuple(source.ref for source in found)
    needle = _collapse_whitespace(quote)
    if needle and any(needle in t for source in found for t in source.texts):
        return Item(text, "quoted", item_refs, needle)
    return Item(text, "cited", item_refs)


# Cap on the target docstring excerpt — keeps the structural section from
# crowding out the change-history evidence.
_DOCSTRING_EXCERPT_CHARS = 240


def _first_paragraph(text: str, limit: int = _DOCSTRING_EXCERPT_CHARS) -> str:
    """Return ``text``'s first paragraph, whitespace-collapsed and clipped.

    Splits on the first blank line, collapses runs of whitespace to single
    spaces, and truncates to ``limit`` characters with an ellipsis marker.
    """
    paragraph = " ".join(text.strip().split("\n\n", 1)[0].split())
    if len(paragraph) > limit:
        return paragraph[:limit].rstrip() + "…"
    return paragraph


def _format_relation(relation: Relation) -> str:
    """Render one caller/callee :class:`Relation` as a single bullet line.

    Falls back to the neighbour symbol's own start line when the edge did not
    record a site line.
    """
    symbol = relation.symbol
    line = relation.line if relation.line is not None else symbol.start_line
    return f"  - {symbol.qualified_name} ({symbol.kind})  {symbol.file_path}:{line}"


def _format_relations(header: str, relations: Sequence[Relation]) -> list[str]:
    """Render a labelled caller/callee block — ``header`` plus one line each.

    An empty ``relations`` yields a single ``(none recorded)`` line so the
    block's meaning stays explicit.
    """
    lines = [header]
    if relations:
        lines.extend(_format_relation(r) for r in relations)
    else:
        lines.append("  (none recorded)")
    return lines


def _format_symbol_context(context: SymbolContext) -> str:
    """Render a :class:`SymbolContext` as the structural-evidence section.

    The block names the target symbol — kind, location, signature, and a
    docstring excerpt — then lists its callers (fan-in) and callees (fan-out).
    It is prepended to the change-history evidence so the model sees *what the
    code is* before *how it got there*.
    """
    target = context.target
    lines = [
        "CODE GRAPH CONTEXT",
        "",
        f"Target: {target.qualified_name} ({target.kind})",
        f"  {target.file_path}:{target.start_line}-{target.end_line}",
    ]
    if target.signature and target.signature.strip():
        # CodeGraph stores signatures verbatim — often multi-line; collapse to
        # one line so the block's indentation stays consistent.
        lines.append(f"  {' '.join(target.signature.split())}")
    if target.docstring and target.docstring.strip():
        lines.append(f"  {_first_paragraph(target.docstring)}")
    lines.append("")
    lines.extend(
        _format_relations(
            f"Called by ({len(context.callers)} caller(s) — blast radius of a change):",
            context.callers,
        )
    )
    lines.append("")
    lines.extend(
        _format_relations(
            f"Calls ({len(context.callees)} callee(s) — what this depends on):",
            context.callees,
        )
    )
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    """Strip a single wrapping Markdown code fence, if present.

    The rationale system prompt asks for raw JSON, but models still wrap
    output in a ``json`` code fence. A leading fence line and a trailing
    fence are removed; text without a fence is returned unchanged.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]  # drop the opening ``` / ```json line
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_rationale_json(text: str) -> dict:
    """Parse and validate the model's rationale output.

    Strips an optional Markdown code fence, decodes the JSON, and checks the
    five-key schema: ``purpose`` and ``why`` are strings; ``tradeoffs`` is a
    list of strings; ``constraints`` and ``risks`` are lists whose items are
    a string or an object with a string ``text`` (their ``refs`` and
    ``quote`` are judged later by :func:`_item` and never raise). Unknown
    keys are ignored.

    Parameters
    ----------
    text : str
        The raw assistant text from :attr:`CompletionResponse.text`.

    Returns
    -------
    dict
        The validated payload — the five schema keys plus any extras.

    Raises
    ------
    RationaleError
        If the text is not a JSON object, or any required key is missing or
        has the wrong type.
    """
    payload = _strip_code_fence(text)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RationaleError(
            f"rationale output is not valid JSON: {exc}; got {payload[:120]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RationaleError(
            f"rationale output must be a JSON object, got {type(parsed).__name__}"
        )
    for key in _REQUIRED_STR_FIELDS:
        if key not in parsed:
            raise RationaleError(f"rationale output missing required key {key!r}")
        if not isinstance(parsed[key], str):
            raise RationaleError(
                f"rationale key {key!r} must be a string, "
                f"got {type(parsed[key]).__name__}"
            )
    for key in _REQUIRED_LIST_FIELDS:
        if key not in parsed:
            raise RationaleError(f"rationale output missing required key {key!r}")
        value = parsed[key]
        if key in _SOURCED_LIST_FIELDS:
            if not isinstance(value, list) or not all(
                isinstance(item, str)
                or (isinstance(item, dict) and isinstance(item.get("text"), str))
                for item in value
            ):
                raise RationaleError(
                    f"rationale key {key!r} must be a list of strings or of "
                    "objects with a string 'text'"
                )
        elif not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RationaleError(f"rationale key {key!r} must be a list of strings")
    return parsed


class RationaleGenerator:
    """Turn an evidence bundle into a structured rationale card.

    The generator validates the bundle is non-empty, renders the
    ``rationale`` prompt, runs one :meth:`LlmClient.complete` call, and
    parses the model's JSON into a :class:`~whygraph.analyze.Rationale`.

    Parameters
    ----------
    client : LlmClient
        Pre-configured adapter. Inject a stub in tests; in production,
        :meth:`from_config` builds one via :class:`LlmClientFactory`.
    timeout_sec : int or None
        Per-call timeout forwarded into the :class:`CompletionRequest`.
        ``None`` (default) defers to the adapter's bound default.
    rationale_prompt : Prompt, optional
        Override the ``rationale`` prompt — the ``system`` + ``task`` pair.
        When ``None`` (default), it is resolved from markdown by the
        client's provider and model — see
        :func:`whygraph.analyze.prompt.resolve`. An explicit
        :class:`~whygraph.analyze.prompt.Prompt` skips resolution; its
        ``task`` should contain the
        :data:`~whygraph.analyze.prompt.RATIONALE_PLACEHOLDER` token. Mostly
        used in tests and one-off overrides.
    caps : _PrRenderCaps, optional
        Size caps for rendering each PR's squashed-commit roster and
        discussion into the prompt. ``None`` (default) uses the
        :class:`~whygraph.core.config.RationaleConfig` defaults;
        :meth:`from_config` projects them from the loaded config.

    Examples
    --------
    >>> generator = RationaleGenerator.from_config(get_config())
    >>> rationale = generator.generate([CommitEvidence(commit)])
    >>> print(rationale.purpose)
    """

    def __init__(
        self,
        client: LlmClient,
        *,
        timeout_sec: int | None = None,
        rationale_prompt: Prompt | None = None,
        caps: _PrRenderCaps | None = None,
    ) -> None:
        self._client = client
        self._timeout_sec = timeout_sec
        self._caps = caps if caps is not None else _PrRenderCaps()
        self._rationale_prompt = (
            rationale_prompt
            if rationale_prompt is not None
            else resolve(_PROMPT_COMPONENT, "rationale", client.provider, client.model)
        )

    def __repr__(self) -> str:
        return f"RationaleGenerator(client={self._client!r})"

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        factory: LlmClientFactory | None = None,
    ) -> "RationaleGenerator":
        """Build a generator from a :class:`Config`.

        The provider and model come from ``config.model_for("rationale")``
        and the timeout from ``config.timeout_for("rationale")``; the PR
        rendering caps from ``config.rationale``.

        Parameters
        ----------
        config : Config
            Typically ``get_config()``.
        factory : LlmClientFactory, optional
            Override the factory used to resolve the rationale provider into
            an :class:`LlmClient`. Defaults to a fresh
            :class:`LlmClientFactory` bound to the process-wide
            :class:`LlmConfig`. Inject a custom factory in tests to bind a
            stub adapter without touching global state.

        Returns
        -------
        RationaleGenerator
            A generator ready to call :meth:`generate`.

        Raises
        ------
        whygraph.services.llm.LlmError
            If the rationale provider is not registered with the factory
            (propagated directly so the user sees the available
            providers), or its :meth:`~whygraph.services.llm.LlmClient.preflight`
            check fails.
        """
        factory = factory if factory is not None else LlmClientFactory()
        provider, model = config.model_for("rationale")
        client = factory.make(provider, model=model)
        client.preflight()
        return cls(
            client,
            timeout_sec=config.timeout_for("rationale"),
            caps=_PrRenderCaps.from_config(config.rationale),
        )

    def generate(
        self,
        evidence: Sequence[CommitEvidence],
        *,
        symbol_context: SymbolContext | None = None,
        subject: str | None = None,
        paths: frozenset[str] | None = None,
        file_lines: Sequence[str] | None = None,
        target_range: tuple[int, int] | None = None,
    ) -> Rationale:
        """Generate a rationale card for one evidence bundle.

        Parameters
        ----------
        evidence : Sequence[CommitEvidence]
            The commits — with their linked pull requests and issues —
            whose history explains the code. Formatted into the prompt in
            the order given.
        symbol_context : SymbolContext, optional
            The target symbol's code-graph context — its signature,
            docstring, callers, and callees. When given, it is rendered as a
            ``CODE GRAPH CONTEXT`` section ahead of the change history, so the
            rationale is grounded in code structure as well as commit prose.
            ``None`` (default) omits the section.
        subject : str, optional
            What the card is about (the qualified name, else the path),
            reported with the call's usage
            (:func:`whygraph.core.usage.record_usage`).
        paths : frozenset[str], optional
            The target file and its rename aliases: review threads on these
            paths may enter the prompt (plan 62 section 4.9a).
        file_lines : Sequence[str], optional
            The target file's current lines, the whole file, to find each
            thread's content anchor in.
        target_range : tuple[int, int], optional
            The target's inclusive line range in ``file_lines``. Review
            threads render only when ``paths``, ``file_lines`` and
            ``target_range`` are all given; any ``None`` keeps them out.

        Returns
        -------
        Rationale
            The model's structured explanation plus provenance.

        Raises
        ------
        AnalyzeError
            If ``evidence`` is empty, or if the underlying
            :meth:`LlmClient.complete` raises :class:`LlmError`. The
            original exception is preserved as ``__cause__``.
        RationaleError
            If the model's output cannot be parsed as the expected
            rationale JSON. Subclasses :class:`AnalyzeError`.
        """
        if not evidence:
            raise AnalyzeError("empty evidence: nothing to explain")

        # TODO: capping bundle size belongs to the future evidence-bundle
        # builder — the generator neither truncates nor chunks its input.
        # One thread selection for both the prompt and the quote sources.
        threads = _select_threads(evidence, paths, file_lines, target_range, self._caps)
        bundle = _format_evidence(evidence, self._caps, threads)
        # The text of each piece of evidence, by id, to judge the items' quotes.
        sources = _evidence_sources(evidence, self._caps, threads)
        if symbol_context is not None:
            bundle = f"{_format_symbol_context(symbol_context)}\n\n{bundle}"
        task = render(
            self._rationale_prompt.task, bundle, placeholder=RATIONALE_PLACEHOLDER
        )
        request = CompletionRequest.of(
            task,
            system=self._rationale_prompt.system,
            timeout_sec=self._timeout_sec,
        )

        started = time.monotonic()
        try:
            response = self._client.complete(request)
        except LlmError as exc:
            raise AnalyzeError(f"LLM call failed: {exc}") from exc
        # Before parsing: a reply that is not valid JSON was still billed.
        record_usage(
            "rationale",
            response,
            provider=self._client.provider,
            model_requested=self._client.model,
            subject=subject,
            started=started,
        )

        fields = _parse_rationale_json(response.text)
        return Rationale(
            purpose=fields["purpose"],
            why=fields["why"],
            constraints=tuple(_item(raw, sources) for raw in fields["constraints"]),
            tradeoffs=tuple(fields["tradeoffs"]),
            risks=tuple(_item(raw, sources) for raw in fields["risks"]),
            model=response.model,
            provider=response.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )
