"""The first-scan cost guard: how much describing the waiting commits would cost.

The first scan of a project runs structure-only (``trigger=initial``,
plan section 4.14). Afterwards ``GET /api/projects/{slug}/scan-estimate``
tells the user how many commits a full scan would send to the LLM, with
which model, and roughly how many tokens (and dollars, for any model the
price table prices - :mod:`whygraph.portal.prices`) - so *Describe now* is
an informed choice.

The count mirrors what the analyze crawler would actually describe
(``scan/analyze_crawler.py``): rows whose ``llm_description`` is NULL,
minus empty commits and PR-origin rows (``first_seen_ref`` under
``refs/pull/``), which stay NULL forever. Reachability is not checked, so
the count is an **upper bound**. A merge's stored stats are zero (``git
log --shortstat`` prints none for it) while the crawler describes its
whole first-parent diff, so merges are sized from git
(:meth:`~whygraph.services.git.Repository.first_parent_stats`). Commits
touching more files than ``large_commit_file_count`` get a cheap stub
instead of an LLM call (their per-file descriptions are generated on
demand), so they are reported separately and cost nothing now.

Arithmetic (:func:`estimate_tokens`, pinned by unit tests and by a
calibration test against a measured run) follows
:meth:`~whygraph.analyze.llm_descriptor.LlmDescriptor.describe`, per
commit:

* diff size = ``files * CHARS_PER_FILE + lines * CHARS_PER_LINE`` chars
  (headers and context per file, content per changed line);
* a diff within ``max_diff_chars`` (or a single file, truncated to it) is
  **one call**: the diff plus :data:`DESCRIBE_OVERHEAD_TOKENS` in,
  :data:`OUTPUT_TOKENS_PER_CALL` out;
* a larger multi-file diff is split into ``ceil(size / max_diff_chars)``
  chunks (at least 2, at most one per file): **one call per chunk** (its
  share of the diff plus the overhead in, :data:`OUTPUT_TOKENS_PER_CHUNK`
  out) **plus a synthesis call** (:data:`SYNTHESIS_OVERHEAD_TOKENS` plus
  every chunk's output in, :data:`SYNTHESIS_OUTPUT_TOKENS` out);
* the range is +/-50 % (:data:`RANGE_FACTOR`), never a promise.

The constants were calibrated on the usage ledger of a real *Describe
now* run (issue #86: 188 commits, 291 calls, ``claude-sonnet-5-5``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import func, or_, select

from whygraph.core.config import Config, ConfigError
from whygraph.db import get_session
from whygraph.db.models import Commit
from whygraph.services.git import DiffStats, GitError, Repository

from .prices import (
    BUNDLED_AS_OF,
    NO_OVERRIDES,
    PriceOverrides,
    cost,
    has_custom_endpoint,
    price_for,
)

CHARS_PER_LINE = 60
"""Average characters per changed diff line (content plus the ``+`` / ``-``)."""

CHARS_PER_FILE = 1_800
"""Diff characters per touched file beyond its changed lines: the ``diff --git``,
``index`` and ``---`` / ``+++`` headers, hunk headers and context lines."""

CHARS_PER_TOKEN = 4
"""The usual chars-per-token rule of thumb."""

DESCRIBE_OVERHEAD_TOKENS = 350
"""Input of one describe call beyond the diff (system and task prompts)."""

OUTPUT_TOKENS_PER_CALL = 1_000
"""Output of a describe call for a diff that fits in one call."""

OUTPUT_TOKENS_PER_CHUNK = 1_500
"""Output of a describe call for one chunk of a split diff."""

SYNTHESIS_OVERHEAD_TOKENS = 400
"""Input of the synthesis call beyond the chunk descriptions it merges."""

SYNTHESIS_OUTPUT_TOKENS = 2_600
"""Output of the synthesis call that merges a split diff's descriptions."""

RANGE_FACTOR = 0.5
"""The estimate is reported as ``[x * (1 - f), x * (1 + f)]``."""

_log = logging.getLogger(__name__)

ORG_PRICES_LABEL = "your organization's prices"
"""``prices_as_of`` when an org price override priced the estimate."""


@dataclass(frozen=True)
class CommitSize:
    """The diff size of one commit waiting for a description."""

    files_changed: int
    insertions: int
    deletions: int


@dataclass(frozen=True)
class TokenEstimate:
    """The token arithmetic's result.

    Attributes
    ----------
    commits : int
        Commits that would get an LLM description.
    large_commits : int
        Commits over ``large_commit_file_count`` (stubbed, no LLM now).
    synthesis_calls : int
        Split commits that need a synthesis call.
    input_tokens, output_tokens : int
        Point estimates.
    calls : int
        LLM calls: one per unsplit commit, chunks plus one per split commit.
    """

    commits: int
    large_commits: int
    synthesis_calls: int
    input_tokens: int
    output_tokens: int
    calls: int = 0


def estimate_tokens(
    sizes: Iterable[CommitSize], *, max_diff_chars: int, large_commit_file_count: int
) -> TokenEstimate:
    """Apply the descriptor's call arithmetic to the waiting commits.

    Parameters
    ----------
    sizes : Iterable[CommitSize]
        One entry per commit that still needs a description (already
        filtered to non-empty, non-PR-origin rows; merges sized by their
        first-parent diff).
    max_diff_chars : int
        ``[analyze].max_diff_chars``.
    large_commit_file_count : int
        ``[analyze].large_commit_file_count``.

    Returns
    -------
    TokenEstimate
        The counts, calls and token totals.
    """
    commits = large = synth = calls = 0
    input_tokens = output_tokens = 0
    for size in sizes:
        if size.files_changed > large_commit_file_count:
            large += 1
            continue
        commits += 1
        chars = (
            size.files_changed * CHARS_PER_FILE
            + (size.insertions + size.deletions) * CHARS_PER_LINE
        )
        chunks = min(size.files_changed, max(2, -(-chars // max_diff_chars)))
        if chars <= max_diff_chars or chunks < 2:
            calls += 1
            sent = min(chars, max_diff_chars)
            input_tokens += sent // CHARS_PER_TOKEN + DESCRIBE_OVERHEAD_TOKENS
            output_tokens += OUTPUT_TOKENS_PER_CALL
            continue
        synth += 1
        calls += chunks + 1
        sent = min(chars, chunks * max_diff_chars)
        chunk_output = chunks * OUTPUT_TOKENS_PER_CHUNK
        input_tokens += (
            sent // CHARS_PER_TOKEN
            + chunks * DESCRIBE_OVERHEAD_TOKENS
            + SYNTHESIS_OVERHEAD_TOKENS
            + chunk_output
        )
        output_tokens += chunk_output + SYNTHESIS_OUTPUT_TOKENS
    return TokenEstimate(
        commits=commits,
        large_commits=large,
        synthesis_calls=synth,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        calls=calls,
    )


def _range(value: float) -> dict:
    return {
        "low": round(value * (1 - RANGE_FACTOR), 4),
        "high": round(value * (1 + RANGE_FACTOR), 4),
    }


def render_estimate(
    tokens: TokenEstimate,
    *,
    provider: str | None,
    model: str | None,
    overrides: PriceOverrides = NO_OVERRIDES,
    custom_endpoint: bool = False,
) -> dict:
    """Shape a :class:`TokenEstimate` for the API (tokens, optional cost, ranges).

    Parameters
    ----------
    tokens : TokenEstimate
        The arithmetic's result.
    provider, model : str or None
        The resolved analyze model.
    overrides : PriceOverrides, optional
        The org's price overrides (none by default).
    custom_endpoint : bool, optional
        Whether the provider's endpoint is overridden; the bundled table
        then does not apply (:func:`~whygraph.portal.prices.price_for`).

    Returns
    -------
    dict
        ``commits``, ``upper_bound``, ``large_commits``, ``model``,
        ``tokens`` (``input`` / ``output`` point values plus a
        ``low`` / ``high`` range each) and ``cost`` (``None`` for an
        unpriced model; ``prices_as_of`` is the bundled table's date, or
        :data:`ORG_PRICES_LABEL` when an org override applied).
    """
    body: dict = {
        "commits": tokens.commits,
        "upper_bound": True,
        "large_commits": tokens.large_commits,
        "model": {"provider": provider, "model": model},
        "tokens": {
            "input": tokens.input_tokens,
            "output": tokens.output_tokens,
            "input_range": _range(tokens.input_tokens),
            "output_range": _range(tokens.output_tokens),
        },
        "cost": None,
    }
    priced = price_for(provider, model, overrides, custom_endpoint=custom_endpoint)
    if priced is not None:
        price, version = priced
        usd = float(cost(price, tokens))
        body["cost"] = {
            "usd": round(usd, 4),
            **_range(usd),
            "currency": "USD",
            "prices_as_of": (
                ORG_PRICES_LABEL if version.startswith("org:") else BUNDLED_AS_OF
            ),
        }
    return body


def waiting_commit_sizes(root: Path) -> list[CommitSize]:
    """Read the commits a full scan would describe from the bound project DB.

    Parameters
    ----------
    root : Path
        The project's repository, read for the merges' first-parent stats.

    Returns
    -------
    list[CommitSize]
        Rows with a NULL ``llm_description`` and a ``first_seen_ref``
        outside ``refs/pull/``: every merge, sized by its first-parent
        diff, and every other row with ``files_changed > 0``. When git
        cannot size the merges (a missing clone), they count with no diff.
    """
    with get_session() as session:
        rows = session.exec(
            select(
                Commit.sha,
                Commit.parent_shas,
                Commit.files_changed,
                Commit.insertions,
                Commit.deletions,
            )
            .where(Commit.llm_description.is_(None))  # type: ignore[union-attr]
            .where(
                or_(
                    Commit.files_changed > 0,
                    func.instr(Commit.parent_shas, " ") > 0,
                )
            )
            .where(func.coalesce(Commit.first_seen_ref, "").not_like("refs/pull/%"))
        ).all()
    merges = [sha for sha, parents, *_ in rows if len(parents.split()) > 1]
    stats: dict[str, DiffStats] = {}
    if merges:
        try:
            stats = Repository(root).first_parent_stats(merges)
        except GitError:
            _log.warning(
                "could not size %d merge commit(s) for the estimate",
                len(merges),
                exc_info=True,
            )
    sizes = []
    for sha, _parents, files, ins, dels in rows:
        if sha in stats:
            merged = stats[sha]
            if not merged.files_changed:
                continue  # an empty first-parent diff is never described
            files, ins, dels = merged.files_changed, merged.insertions, merged.deletions
        sizes.append(CommitSize(files, ins, dels))
    return sizes


def scan_estimate(
    config: Config, *, root: Path, overrides: PriceOverrides = NO_OVERRIDES
) -> dict:
    """The ``scan-estimate`` body for the bound project (blocking).

    Parameters
    ----------
    config : Config
        The project's resolved config (analyze model and limits).
    root : Path
        The project's repository (merges are sized from git).
    overrides : PriceOverrides, optional
        The project's org price overrides (empty by default; the route
        passes the org's from the portal's
        :class:`~whygraph.portal.usage_store.PriceBook`).

    Returns
    -------
    dict
        :func:`render_estimate`'s body; ``missing_key`` is added by the
        caller.
    """
    try:
        choice = config.model_for("analyze")
        provider, model = choice.provider, choice.model
    except ConfigError:
        provider = model = None
    tokens = estimate_tokens(
        waiting_commit_sizes(root),
        max_diff_chars=config.analyze.max_diff_chars,
        large_commit_file_count=config.analyze.large_commit_file_count,
    )
    return render_estimate(
        tokens,
        provider=provider,
        model=model,
        overrides=overrides,
        custom_endpoint=has_custom_endpoint(config, provider),
    )


PURPOSE_INPUT_TOKENS = 8_000
"""A project-purpose generation's input: the 24 KB of gathered text plus the prompt."""

PURPOSE_OUTPUT_TOKENS = 400
"""A project-purpose generation's output: four short labelled lines."""


def purpose_estimate(
    config: Config, *, overrides: PriceOverrides = NO_OVERRIDES
) -> dict:
    """What one project-purpose generation would cost (plan 41 section 5.3).

    A fixed :data:`PURPOSE_INPUT_TOKENS` in and :data:`PURPOSE_OUTPUT_TOKENS`
    out on the ``[analyze]`` model, priced through :func:`render_estimate`
    (so the same ``model`` / ``tokens`` / ``cost`` shape and +/-50 % range
    as ``scan-estimate``), without the commit counts that only a scan has.

    Parameters
    ----------
    config : Config
        The project's resolved config (the analyze model).
    overrides : PriceOverrides, optional
        The project's org price overrides.

    Returns
    -------
    dict
        ``model``, ``tokens`` and ``cost`` as :func:`render_estimate`;
        ``missing_key`` and ``cost_hidden`` are added by the caller.
    """
    try:
        choice = config.model_for("analyze")
        provider, model = choice.provider, choice.model
    except ConfigError:
        provider = model = None
    tokens = TokenEstimate(
        commits=0,
        large_commits=0,
        synthesis_calls=0,
        input_tokens=PURPOSE_INPUT_TOKENS,
        output_tokens=PURPOSE_OUTPUT_TOKENS,
    )
    body = render_estimate(
        tokens,
        provider=provider,
        model=model,
        overrides=overrides,
        custom_endpoint=has_custom_endpoint(config, provider),
    )
    for key in ("commits", "upper_bound", "large_commits"):
        body.pop(key, None)
    return body


def compact_estimate(body: dict, missing_key: str | None) -> dict:
    """The compact estimate a full run stores in ``summary.estimate``.

    Parameters
    ----------
    body : dict
        :func:`scan_estimate`'s (or :func:`render_estimate`'s) body.
    missing_key : str or None
        The analyze provider when it has no key (the ``scan-estimate``
        route's ``missing_key``).

    Returns
    -------
    dict
        ``{commits, model: {provider, model}, cost_usd, cost_low_usd,
        cost_high_usd, prices_as_of, missing_key}``; the ``cost_*`` values
        and ``prices_as_of`` are ``None`` for an unpriced model (M2f-3 plan
        section 4.1).
    """
    priced = body.get("cost") or {}
    return {
        "commits": body["commits"],
        "model": dict(body["model"]),
        "cost_usd": priced.get("usd"),
        "cost_low_usd": priced.get("low"),
        "cost_high_usd": priced.get("high"),
        "prices_as_of": priced.get("prices_as_of"),
        "missing_key": missing_key,
    }


__all__ = [
    "CHARS_PER_FILE",
    "CHARS_PER_LINE",
    "CHARS_PER_TOKEN",
    "DESCRIBE_OVERHEAD_TOKENS",
    "ORG_PRICES_LABEL",
    "OUTPUT_TOKENS_PER_CALL",
    "OUTPUT_TOKENS_PER_CHUNK",
    "PURPOSE_INPUT_TOKENS",
    "PURPOSE_OUTPUT_TOKENS",
    "RANGE_FACTOR",
    "SYNTHESIS_OUTPUT_TOKENS",
    "SYNTHESIS_OVERHEAD_TOKENS",
    "CommitSize",
    "TokenEstimate",
    "compact_estimate",
    "estimate_tokens",
    "purpose_estimate",
    "render_estimate",
    "scan_estimate",
    "waiting_commit_sizes",
]
