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
the count is an **upper bound**. Commits touching more files than
``large_commit_file_count`` get a cheap stub instead of an LLM call (their
per-file descriptions are generated on demand), so they are reported
separately and cost nothing now.

Arithmetic (:func:`estimate_tokens`, pinned by a unit test):

* input tokens = ``(insertions + deletions) * CHARS_PER_LINE / CHARS_PER_TOKEN``
  per commit, with **no** cap - a multi-file diff over ``max_diff_chars``
  is chunked, not truncated - except that a single-file commit is capped
  at ``max_diff_chars`` (one chunk is truncated);
* plus one synthesis call (:data:`SYNTHESIS_INPUT_TOKENS`) per multi-file
  commit over ``max_diff_chars``;
* output tokens = :data:`OUTPUT_TOKENS_PER_COMMIT` per commit;
* the range is +/-50 % (:data:`RANGE_FACTOR`), never a promise.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlmodel import func, select

from whygraph.core.config import Config, ConfigError
from whygraph.db import get_session
from whygraph.db.models import Commit

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

CHARS_PER_TOKEN = 4
"""The usual chars-per-token rule of thumb."""

OUTPUT_TOKENS_PER_COMMIT = 300
"""A commit description's typical length in output tokens."""

SYNTHESIS_INPUT_TOKENS = 1_500
"""Input of the synthesis call that merges a chunked diff's descriptions."""

RANGE_FACTOR = 0.5
"""The estimate is reported as ``[x * (1 - f), x * (1 + f)]``."""

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
        Chunked commits that need a synthesis call.
    input_tokens, output_tokens : int
        Point estimates.
    """

    commits: int
    large_commits: int
    synthesis_calls: int
    input_tokens: int
    output_tokens: int


def estimate_tokens(
    sizes: Iterable[CommitSize], *, max_diff_chars: int, large_commit_file_count: int
) -> TokenEstimate:
    """Apply the section 4.14 arithmetic to the waiting commits.

    Parameters
    ----------
    sizes : Iterable[CommitSize]
        One entry per commit that still needs a description (already
        filtered to non-empty, non-PR-origin rows).
    max_diff_chars : int
        ``[analyze].max_diff_chars``.
    large_commit_file_count : int
        ``[analyze].large_commit_file_count``.

    Returns
    -------
    TokenEstimate
        The counts and token totals.
    """
    commits = large = synth = 0
    input_chars = 0
    for size in sizes:
        if size.files_changed > large_commit_file_count:
            large += 1
            continue
        commits += 1
        chars = (size.insertions + size.deletions) * CHARS_PER_LINE
        if size.files_changed == 1:
            chars = min(chars, max_diff_chars)
        elif chars > max_diff_chars:
            synth += 1
        input_chars += chars
    input_tokens = input_chars // CHARS_PER_TOKEN + synth * SYNTHESIS_INPUT_TOKENS
    return TokenEstimate(
        commits=commits,
        large_commits=large,
        synthesis_calls=synth,
        input_tokens=input_tokens,
        output_tokens=commits * OUTPUT_TOKENS_PER_COMMIT,
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


def waiting_commit_sizes() -> list[CommitSize]:
    """Read the commits a full scan would describe from the bound project DB.

    Returns
    -------
    list[CommitSize]
        Rows with a NULL ``llm_description``, ``files_changed > 0`` and a
        ``first_seen_ref`` outside ``refs/pull/``.
    """
    with get_session() as session:
        rows = session.exec(
            select(Commit.files_changed, Commit.insertions, Commit.deletions)
            .where(Commit.llm_description.is_(None))  # type: ignore[union-attr]
            .where(Commit.files_changed > 0)
            .where(func.coalesce(Commit.first_seen_ref, "").not_like("refs/pull/%"))
        ).all()
    return [CommitSize(f, i, d) for f, i, d in rows]


def scan_estimate(config: Config, *, overrides: PriceOverrides = NO_OVERRIDES) -> dict:
    """The ``scan-estimate`` body for the bound project (blocking).

    Parameters
    ----------
    config : Config
        The project's resolved config (analyze model and limits).
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
        waiting_commit_sizes(),
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


__all__ = [
    "CHARS_PER_LINE",
    "CHARS_PER_TOKEN",
    "ORG_PRICES_LABEL",
    "OUTPUT_TOKENS_PER_COMMIT",
    "RANGE_FACTOR",
    "SYNTHESIS_INPUT_TOKENS",
    "CommitSize",
    "TokenEstimate",
    "estimate_tokens",
    "render_estimate",
    "scan_estimate",
    "waiting_commit_sizes",
]
