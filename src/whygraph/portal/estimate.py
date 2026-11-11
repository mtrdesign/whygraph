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
(:meth:`~whygraph.services.git.Repository.first_parent_numstat`); every
other commit is sized from its ``commit_file_change`` rows. Commits
touching more files than ``large_commit_file_count`` get a cheap stub
instead of an LLM call (their per-file descriptions are generated on
demand), so they are reported separately and cost nothing now.

Arithmetic (:func:`estimate_tokens`, pinned by unit tests and by a
calibration test against a measured run) replays
:meth:`~whygraph.analyze.llm_descriptor.LlmDescriptor.describe` per
commit:

* each file's diff is ``CHARS_PER_FILE + lines * CHARS_PER_LINE`` chars
  (headers and context, plus the changed lines);
* the files are packed in order into chunks of at most
  ``max_diff_chars``, as :func:`~whygraph.analyze.diff_split.split_into_chunks`
  does; a lone chunk over the cap is truncated to it;
* one chunk is **one call**; several are **one call per chunk plus a
  synthesis call**;
* a describe call reads ``chars / CHARS_PER_TOKEN +
  DESCRIBE_OVERHEAD_TOKENS`` and writes ``OUTPUT_TOKENS_BASE +
  chars * OUTPUT_TOKENS_PER_1K_CHARS / 1000``; a synthesis call reads
  :data:`SYNTHESIS_OVERHEAD_TOKENS` plus every chunk's output and writes
  ``SYNTHESIS_OUTPUT_TOKENS + chunks * SYNTHESIS_OUTPUT_PER_CHUNK``;
* a model's own tokenizer and verbosity scale the input and output
  (:data:`MODEL_PROFILES`; a model without a profile is estimated as
  ``claude-sonnet-5-5``);
* the range is +/-50 % (:data:`RANGE_FACTOR`), never a promise.

The constants are fitted on ``claude-sonnet-5-5`` over two repositories:
the usage ledger of a whole *Describe now* run of WhyGraph (issue #86,
188 commits) and a 60-commit sample of Biome
(``scripts/estimate_calibration.py``), within 5 % of both.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import func, or_, select

from whygraph.core.config import Config, ConfigError
from whygraph.db import get_session
from whygraph.db.models import (
    Commit,
    CommitFileChange,
    CoreBulkPair,
    CoreCommit,
    CoreState,
)
from whygraph.services.git import GitError, Repository

from .prices import (
    BUNDLED_AS_OF,
    NO_OVERRIDES,
    PriceOverrides,
    cost,
    has_custom_endpoint,
    price_for,
)

CHARS_PER_FILE = 900
"""Diff characters per touched file beyond its changed lines: the ``diff --git``,
``index`` and ``---`` / ``+++`` headers, hunk headers and context lines."""

CHARS_PER_LINE = 40
"""Diff characters per changed line (content plus the ``+`` / ``-``)."""

CHARS_PER_TOKEN = 2.31
"""Diff characters per input token (code tokenizes densely; prose is about 4)."""

DESCRIBE_OVERHEAD_TOKENS = 500
"""Input of one describe call beyond the diff (system and task prompts)."""

OUTPUT_TOKENS_BASE = 400
"""Output of a describe call before it grows with the diff it describes."""

OUTPUT_TOKENS_PER_1K_CHARS = 23.4
"""Extra output of a describe call per 1,000 characters of diff it reads."""

SYNTHESIS_OVERHEAD_TOKENS = 150
"""Input of the synthesis call beyond the chunk descriptions it merges."""

SYNTHESIS_OUTPUT_TOKENS = 500
"""Output of the synthesis call before it grows with the number of chunks."""

SYNTHESIS_OUTPUT_PER_CHUNK = 840
"""Extra output of the synthesis call per chunk it merges."""

RANGE_FACTOR = 0.5
"""The estimate is reported as ``[x * (1 - f), x * (1 + f)]``."""

_log = logging.getLogger(__name__)

ORG_PRICES_LABEL = "your organization's prices"
"""``prices_as_of`` when an org price override priced the estimate."""


@dataclass(frozen=True)
class CommitSize:
    """The diff size of one commit waiting for a description.

    Attributes
    ----------
    files_changed : int
        The file count the crawler's large-commit check sees.
    file_lines : tuple[int, ...]
        Changed lines (insertions plus deletions) of each file, in diff
        order.
    """

    files_changed: int
    file_lines: tuple[int, ...]


@dataclass(frozen=True)
class ModelProfile:
    """How one model differs from the ``claude-sonnet-5-5`` the constants fit.

    Attributes
    ----------
    input_factor : float
        Its input tokens per Sonnet input token (its tokenizer).
    output_factor : float
        Its output tokens per Sonnet output token, on the same diffs (how
        much it writes).
    """

    input_factor: float = 1.0
    output_factor: float = 1.0


BASELINE_PROFILE = ModelProfile()
"""``claude-sonnet-5-5``'s profile, and every model's without a measured one."""

MODEL_PROFILES: dict[str, ModelProfile] = {
    "claude-sonnet-5-5": BASELINE_PROFILE,
    "claude-haiku-4-5": ModelProfile(input_factor=0.78, output_factor=0.32),
    "claude-opus-4-7": ModelProfile(input_factor=1.00, output_factor=0.75),
}
"""Measured profiles by model id: each model described the same Biome commits
as ``claude-sonnet-5-5`` (``scripts/estimate_calibration.py``, 2026-11-09).
Measure a model with that script before adding it here."""


def model_profile(model: str | None) -> ModelProfile:
    """The profile of ``model``, matched by id under any provider prefix.

    Parameters
    ----------
    model : str or None
        A model id, bare (``claude-haiku-4-5``) or with a vendor prefix as
        OpenRouter writes it (``anthropic/claude-haiku-4-5``).

    Returns
    -------
    ModelProfile
        Its measured profile, or :data:`BASELINE_PROFILE`.
    """
    if not model:
        return BASELINE_PROFILE
    return MODEL_PROFILES.get(model.rsplit("/", 1)[-1], BASELINE_PROFILE)


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
    sizes: Iterable[CommitSize],
    *,
    max_diff_chars: int,
    large_commit_file_count: int,
    profile: ModelProfile = BASELINE_PROFILE,
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
    profile : ModelProfile, optional
        The analyze model's profile (:func:`model_profile`).

    Returns
    -------
    TokenEstimate
        The counts, calls and token totals.
    """
    commits = large = synth = calls = 0
    input_tokens = output_tokens = 0.0
    for size in sizes:
        if size.files_changed > large_commit_file_count:
            large += 1
            continue
        commits += 1
        chunks = _pack(
            [CHARS_PER_FILE + lines * CHARS_PER_LINE for lines in size.file_lines]
            or [CHARS_PER_FILE],
            max_diff_chars,
        )
        described = [
            _describe_call(min(chars, max_diff_chars), profile) for chars in chunks
        ]
        calls += len(described)
        input_tokens += sum(tokens_in for tokens_in, _ in described)
        output_tokens += sum(tokens_out for _, tokens_out in described)
        if len(chunks) > 1:
            synth += 1
            calls += 1
            input_tokens += SYNTHESIS_OVERHEAD_TOKENS * profile.input_factor + sum(
                tokens_out for _, tokens_out in described
            )
            output_tokens += profile.output_factor * (
                SYNTHESIS_OUTPUT_TOKENS + len(chunks) * SYNTHESIS_OUTPUT_PER_CHUNK
            )
    return TokenEstimate(
        commits=commits,
        large_commits=large,
        synthesis_calls=synth,
        input_tokens=round(input_tokens),
        output_tokens=round(output_tokens),
        calls=calls,
    )


def _pack(file_chars: list[int], max_chars: int) -> list[int]:
    """Pack file sizes in order into chunks, as ``split_into_chunks`` does."""
    chunks: list[int] = []
    current = 0
    for chars in file_chars:
        if current and current + chars > max_chars:
            chunks.append(current)
            current = chars
        else:
            current += chars
    chunks.append(current)
    return chunks


def _describe_call(chars: int, profile: ModelProfile) -> tuple[float, float]:
    """One describe call's (input, output) tokens for ``chars`` of diff."""
    return (
        profile.input_factor * (chars / CHARS_PER_TOKEN + DESCRIBE_OVERHEAD_TOKENS),
        profile.output_factor
        * (OUTPUT_TOKENS_BASE + chars * OUTPUT_TOKENS_PER_1K_CHARS / 1_000),
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


def waiting_commit_sizes(
    root: Path, *, max_diff_chars: int, large_commit_file_count: int
) -> list[CommitSize]:
    """The sizes of :func:`waiting_commits`, without their SHAs.

    Parameters
    ----------
    root : Path
        The project's repository.
    max_diff_chars, large_commit_file_count : int
        ``[analyze]``'s chunk cap and large-commit threshold.

    Returns
    -------
    list[CommitSize]
        One entry per waiting commit.
    """
    return list(
        waiting_commits(
            root,
            max_diff_chars=max_diff_chars,
            large_commit_file_count=large_commit_file_count,
        ).values()
    )


def waiting_commits(
    root: Path, *, max_diff_chars: int, large_commit_file_count: int
) -> dict[str, CommitSize]:
    """Read the commits a full scan would describe from the bound project DB.

    Only a commit that may split into chunks needs its files' sizes (a
    diff within ``max_diff_chars`` costs the same however its lines spread
    over its files), so only those read their ``commit_file_change`` rows;
    a merge over ``large_commit_file_count`` files (stubbed) is only
    counted, which is far cheaper for git than sizing its files.

    Parameters
    ----------
    root : Path
        The project's repository, read for the merges' first-parent diffs.
    max_diff_chars, large_commit_file_count : int
        ``[analyze]``'s chunk cap and large-commit threshold.

    Returns
    -------
    dict[str, CommitSize]
        By SHA: rows with a NULL ``llm_description`` and a ``first_seen_ref``
        outside ``refs/pull/``: every merge with a non-empty first-parent
        diff, and every other row with ``files_changed > 0``. Where no
        per-file sizes were read, a commit's lines spread evenly over its
        files; when git cannot read the merges (a missing clone), they
        count as one empty file each.
    """
    waiting = (
        (Commit.llm_description.is_(None))  # type: ignore[union-attr]
        & or_(Commit.files_changed > 0, func.instr(Commit.parent_shas, " ") > 0)
        & func.coalesce(Commit.first_seen_ref, "").not_like("refs/pull/%")
    )
    may_split = (
        Commit.files_changed.between(2, large_commit_file_count)  # type: ignore[attr-defined]
        & (
            Commit.files_changed * CHARS_PER_FILE
            + (Commit.insertions + Commit.deletions) * CHARS_PER_LINE
            > max_diff_chars
        )
    )
    with get_session() as session:
        rows = session.exec(
            select(
                Commit.sha,
                Commit.parent_shas,
                Commit.files_changed,
                Commit.insertions,
                Commit.deletions,
            ).where(waiting)
        ).all()
        per_file: dict[str, list[int]] = {}
        for sha, lines in session.exec(
            select(
                CommitFileChange.commit_sha,
                CommitFileChange.lines_added + CommitFileChange.lines_deleted,
            )
            .join(Commit, Commit.sha == CommitFileChange.commit_sha)  # type: ignore[arg-type]
            .where(waiting & may_split)
            .order_by(CommitFileChange.id)  # type: ignore[arg-type]
        ).all():
            per_file.setdefault(sha, []).append(lines)
    merges = [sha for sha, parents, *_ in rows if len(parents.split()) > 1]
    merge_sizes = _merge_sizes(root, merges, large_commit_file_count)
    sizes: dict[str, CommitSize] = {}
    for sha, _parents, files, ins, dels in rows:
        if sha in merge_sizes:
            if merge_sizes[sha].files_changed:  # an empty diff is never described
                sizes[sha] = merge_sizes[sha]
        elif sha in per_file:
            sizes[sha] = CommitSize(files, tuple(per_file[sha]))
        else:
            sizes[sha] = CommitSize(files, _spread(ins + dels, files))
    return sizes


_MERGE_SIZES: dict[tuple[str, str], tuple[CommitSize, bool]] = {}
"""Merge sizes already read, by ``(repo root, sha)``, with whether their
per-file lines were read: a first-parent diff never changes, and counting a
huge merge's files takes git a while. A merge counted as large (lines not
read) is read again if a higher threshold makes it small."""

_MERGE_SIZES_MAX = 100_000
"""Entries :data:`_MERGE_SIZES` holds before it is cleared."""


def _merge_sizes(
    root: Path, merges: list[str], large_commit_file_count: int
) -> dict[str, CommitSize]:
    """Size merges from git: count every one, read lines only for those described."""
    key = str(root.resolve())
    known: dict[str, CommitSize] = {}
    for sha in merges:
        size, lines_read = _MERGE_SIZES.get((key, sha), (None, False))
        if size and (lines_read or size.files_changed > large_commit_file_count):
            known[sha] = size
    todo = [sha for sha in merges if sha not in known]
    if not todo:
        return known
    repository = Repository(root)
    try:
        counts = repository.first_parent_file_counts(todo)
        small = [sha for sha, n in counts.items() if n <= large_commit_file_count]
        lines = repository.first_parent_numstat(small) if small else {}
    except GitError:
        _log.warning(
            "could not size %d merge commit(s) for the estimate",
            len(todo),
            exc_info=True,
        )
        return known
    if len(_MERGE_SIZES) + len(counts) > _MERGE_SIZES_MAX:
        _MERGE_SIZES.clear()
    for sha, n in counts.items():
        size = CommitSize(n, lines.get(sha, _spread(0, n)))
        _MERGE_SIZES[(key, sha)] = (size, sha in lines)
        known[sha] = size
    return known


def _spread(lines: int, files: int) -> tuple[int, ...]:
    """``lines`` spread as evenly as possible over ``files`` (at least one)."""
    files = max(files, 1)
    share, extra = divmod(lines, files)
    return (share + 1,) * extra + (share,) * (files - extra)


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
        :func:`render_estimate`'s body (= Describe all; tokens and cost
        include pending core bulk pairs) plus ``file_slices``, ``core``
        (the same figure over the core commits, with ``files``,
        ``selection``, ``as_of`` and ``completed_head``; ``None`` until a
        core pass has completed) and ``core_progress``; ``missing_key`` is
        added by the caller.
    """
    try:
        choice = config.model_for("analyze")
        provider, model = choice.provider, choice.model
    except ConfigError:
        provider = model = None
    limits = {
        "max_diff_chars": config.analyze.max_diff_chars,
        "large_commit_file_count": config.analyze.large_commit_file_count,
    }
    profile = model_profile(model)
    sizes = waiting_commits(root, **limits)
    pair_sizes = _pending_pair_sizes()
    with get_session() as session:
        core_shas = set(session.exec(select(CoreCommit.sha)).all())
        state = session.get(CoreState, 1)
        if state is not None:
            session.expunge(state)

    def render(values: list[CommitSize]) -> dict:
        body = render_estimate(
            estimate_tokens([*values, *pair_sizes], **limits, profile=profile),
            provider=provider,
            model=model,
            overrides=overrides,
            custom_endpoint=has_custom_endpoint(config, provider),
        )
        # estimate_tokens counts a pair as a commit; the body's count is commits.
        body["commits"] -= len(pair_sizes)
        body["file_slices"] = len(pair_sizes)
        return body

    body = render(list(sizes.values()))
    body["core"] = None
    body["core_progress"] = None
    if state is not None and state.completed_at is None:
        body["core_progress"] = {"done": state.files_done, "total": state.files_total}
    if state is not None and state.completed_at is not None:
        core = render([size for sha, size in sizes.items() if sha in core_shas])
        core.update(
            files=state.files_selected,
            selection=state.selection,
            as_of=state.completed_at,
            completed_head=state.completed_head,
        )
        body["core"] = core
    return body


def _pending_pair_sizes() -> list[CommitSize]:
    """One single-file size per core bulk pair that has no description yet."""
    with get_session() as session:
        rows = session.exec(
            select(CommitFileChange.lines_added + CommitFileChange.lines_deleted)
            .join(
                CoreBulkPair,
                (CoreBulkPair.sha == CommitFileChange.commit_sha)  # type: ignore[arg-type]
                & (CoreBulkPair.path == CommitFileChange.path),  # type: ignore[arg-type]
            )
            .where(CommitFileChange.llm_description.is_(None))  # type: ignore[union-attr]
        ).all()
    return [CommitSize(files_changed=1, file_lines=(lines,)) for lines in rows]


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
    "BASELINE_PROFILE",
    "CHARS_PER_FILE",
    "CHARS_PER_LINE",
    "CHARS_PER_TOKEN",
    "DESCRIBE_OVERHEAD_TOKENS",
    "MODEL_PROFILES",
    "ORG_PRICES_LABEL",
    "OUTPUT_TOKENS_BASE",
    "OUTPUT_TOKENS_PER_1K_CHARS",
    "PURPOSE_INPUT_TOKENS",
    "PURPOSE_OUTPUT_TOKENS",
    "RANGE_FACTOR",
    "SYNTHESIS_OUTPUT_PER_CHUNK",
    "SYNTHESIS_OUTPUT_TOKENS",
    "SYNTHESIS_OVERHEAD_TOKENS",
    "CommitSize",
    "ModelProfile",
    "TokenEstimate",
    "compact_estimate",
    "estimate_tokens",
    "model_profile",
    "purpose_estimate",
    "render_estimate",
    "scan_estimate",
    "waiting_commit_sizes",
    "waiting_commits",
]
