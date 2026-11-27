"""The rationale depth estimate: prompt sizes per level, priced at read time.

Plan 62 section 14.5a (R33). The route
(:mod:`whygraph.portal.depth_profile_routes`) answers *about how much does a
card at each level cost in this project?* without a single model call:

1. :func:`sample_profile` picks up to :data:`SAMPLE_TARGETS` targets - the
   largest definable symbol of each of the most-referenced files of the
   CodeGraph index - collects each one's evidence and builds, for every
   offered level, the draft prompt a card would send (the generator's own
   formatters through :meth:`~whygraph.analyze.rationale_generator.RationaleGenerator.prompt_chars`
   and :func:`~whygraph.analyze.rationale_generator.card_bundle_for`). That
   is blocking git and file work (6-7 s measured for 20 targets), so it
   runs once per project and ``HEAD`` off the request, single-flight
   (:class:`DepthProfiles`, on ``PortalState`` like ``ContextCache``).
2. :func:`render_profile` turns the cached token counts into the answer:
   per level the p50 and p90 input tokens and a price range for the
   project's rationale model, the cost of an undescribed commit and the
   cheaper-model line (R38). Prices change with the model and the org's
   overrides, so they are applied at read time, never cached.

Token counts are characters divided by :data:`CHARS_PER_TOKEN`: an
estimate, labelled as one everywhere it is shown.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func
from sqlmodel import col, select

from whygraph.analyze.card_bundle import verifies
from whygraph.analyze.rationale_generator import (
    VERIFY_SOURCE_MAX_CHARS,
    RationaleGenerator,
    _PrRenderCaps,
    card_bundle_for,
    verification_source,
)
from whygraph.core.config import OFFERED_DEPTH_LEVELS, Config, ConfigError
from whygraph.db import get_session as project_session
from whygraph.db.models import Commit
from whygraph.mcp.evidence import evidence_from_hunks, review_scope
from whygraph.mcp.rationale import _thread_inputs
from whygraph.mcp.targets import Target
from whygraph.scan.core_crawler import CORE_EDGE_KINDS
from whygraph.services.codegraph import CodeGraph, CodeGraphError
from whygraph.services.git import GitError, Repository
from whygraph.services.llm import LlmError

from .db import get_session
from .estimate import CommitSize, estimate_tokens, model_profile
from .models import UsageEvent
from .prices import BUNDLED_AS_OF, PriceOverrides, cost, has_custom_endpoint, price_for

_log = logging.getLogger(__name__)

SAMPLE_TARGETS = 20
"""Targets sampled: the largest definable symbol of each of the most-referenced files."""

CANDIDATE_FILES = 60
"""Most-referenced files looked at to find :data:`SAMPLE_TARGETS` usable targets."""

WALL_SEC = 60.0
"""Seconds after which sampling stops and the targets so far are used."""

CHARS_PER_TOKEN = 4.0
"""Prompt characters per input token (an estimate; code and prose differ)."""

DEFAULT_DRAFT_OUTPUT_TOKENS = 1500
"""A draft's output tokens when the project has no rationale usage yet."""

VERIFY_OUTPUT_TOKENS = 300
"""A verification pass's output tokens."""

VERIFY_OVERHEAD_CHARS = 2500
"""The verification prompt beyond the target's source (system, task, numbered claims)."""

DEFAULT_DESCRIPTION_CHARS = 600
"""A commit's description length when the project has none to measure."""

DESCRIPTION_SAMPLE = 200
"""Newest usage rows read for a median."""

_DEFINABLE = ("function", "method", "class")


class _NoCallClient:
    """A stand-in client: it names a provider and model for the prompt and never calls."""

    def __init__(self, provider: str, model: str | None) -> None:
        self.provider = provider
        self.model = model

    def complete(self, request: Any) -> Any:
        """Refuse: the estimate makes no model call."""
        raise LlmError("the depth estimate makes no model call")


@dataclass
class _Entry:
    """One project's estimate: computing, ready or failed, for one ``HEAD``."""

    head: str | None
    status: str = "computing"
    raw: dict | None = None
    error: str | None = None
    started: float = field(default_factory=time.monotonic)


class DepthProfiles:
    """Per-project estimate cache and single-flight (a dict on ``PortalState``).

    One entry per project id, replaced when ``HEAD`` moves. At most one
    computation per project runs; a failed entry is handed out once and
    dropped, so the next ask (the UI's Retry) computes again.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, _Entry] = {}

    def acquire(self, project_id: int, head: str | None) -> tuple[_Entry, bool]:
        """The entry for ``project_id`` at ``head``, and whether the caller must compute it.

        Parameters
        ----------
        project_id : int
            The project.
        head : str or None
            Its current ``HEAD``.

        Returns
        -------
        tuple[_Entry, bool]
            The entry and ``True`` when it was just created (the caller
            starts the computation); ``False`` for a computing, ready or
            failed one already there.
        """
        with self._lock:
            entry = self._entries.get(project_id)
            if entry is not None and entry.head == head:
                if entry.status == "error":
                    del self._entries[project_id]  # handed out once: Retry computes
                return entry, False
            entry = self._entries[project_id] = _Entry(head)
            return entry, True

    def finish(
        self, project_id: int, entry: _Entry, raw: dict | None, error: str | None
    ) -> None:
        """Record the outcome of a computation (ignored if the entry was replaced)."""
        with self._lock:
            if self._entries.get(project_id) is not entry:
                return
            entry.raw, entry.error = raw, error
            entry.status = "error" if error else "ready"

    def discard(self, project_id: int) -> None:
        """Drop a project's entry (the project was removed)."""
        with self._lock:
            self._entries.pop(project_id, None)


# ---- sampling -------------------------------------------------------------


def _percentile(values: list[float], fraction: float) -> float:
    """The nearest-rank percentile of ``values`` (not empty)."""
    ordered = sorted(values)
    return ordered[max(math.ceil(fraction * len(ordered)) - 1, 0)]


def _largest_symbol(graph: CodeGraph, path: str):
    """The largest function, method or class defined in ``path``, or ``None``."""
    symbols = [
        s for s in graph.area(path) if s.kind in _DEFINABLE and s.file_path == path
    ]
    return max(
        symbols,
        key=lambda s: (s.end_line - s.start_line, s.qualified_name),
        default=None,
    )


def sample_profile(
    root: Path,
    config: Config,
    *,
    git_budget: int,
    median_description_chars: int,
    wall_sec: float = WALL_SEC,
) -> dict:
    """Build the draft prompt of each offered level over a sample of targets (blocking).

    Must run under the project's context (``use_project``): evidence reads
    the project's WhyGraph database and ``review_scope`` its working tree.
    Makes no model call.

    Parameters
    ----------
    root : Path
        The project's repository.
    config : Config
        The project's resolved config (the ``[rationale]`` caps, the
        CodeGraph database).
    git_budget : int
        Blame calls one target's evidence may make.
    median_description_chars : int
        Added per commit the prompt shows without a description, so a
        card's size is that of a described one.
    wall_sec : float, optional
        The sampling wall; default :data:`WALL_SEC`.

    Returns
    -------
    dict
        ``{"status": "no_index"}`` without a usable CodeGraph index,
        ``{"status": "no_history"}`` when no sampled target has scanned
        history to build a prompt from, else
        ``{"status": "ready", "targets": [...], "sampled", "computed_at"}``
        where each target maps every offered level to
        ``{"input_tokens", "undescribed", "commits"}``, and
        ``"verify_input_tokens"`` for the levels that verify.

    """
    deadline = time.monotonic() + wall_sec
    try:
        graph = CodeGraph.for_repository(root, codegraph_db=config.codegraph_db)
    except CodeGraphError:
        return {"status": "no_index"}
    repo = Repository(root)
    caps = _PrRenderCaps.from_config(config.rationale)
    choice = config.model_for("rationale")
    generator = RationaleGenerator(
        _NoCallClient(choice.provider, choice.model), caps=caps
    )
    targets: list[dict] = []
    with graph:
        ranked = sorted(
            graph.file_in_degree(CORE_EDGE_KINDS).items(),
            key=lambda kv: (-kv[1], kv[0]),
        )[:CANDIDATE_FILES]
        for path, _ in ranked:
            if len(targets) >= SAMPLE_TARGETS or time.monotonic() > deadline:
                break
            symbol = _largest_symbol(graph, path)
            if symbol is None:
                continue
            target = Target(
                path=path,
                line_start=symbol.start_line,
                line_end=symbol.end_line,
                qualified_name=symbol.qualified_name,
            )
            try:
                measured = _measure(
                    repo,
                    graph,
                    generator,
                    config,
                    target,
                    git_budget,
                    median_description_chars,
                )
            except (GitError, CodeGraphError):
                continue
            if measured is not None:
                targets.append(measured)
    if not targets:
        return {"status": "no_history"}
    return {
        "status": "ready",
        "targets": targets,
        "sampled": len(targets),
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _measure(
    repo: Repository,
    graph: CodeGraph,
    generator: RationaleGenerator,
    config: Config,
    target: Target,
    git_budget: int,
    median_description_chars: int,
) -> dict | None:
    """One target's prompt size per offered level, or ``None`` without evidence."""
    hunks = repo.blame(target.path, target.line_start, target.line_end)
    evidence = evidence_from_hunks(
        repo, target, hunks, limit=20, git_budget=git_budget
    ).evidence
    if not evidence:
        return None
    with project_session() as session:
        scope = review_scope(session, repo.root, target.path)
    inputs = _thread_inputs(scope, (target.line_start, target.line_end))
    context = graph.context(target.qualified_name) if target.qualified_name else None
    source, _ = verification_source(
        scope.current_lines,
        (target.line_start, target.line_end),
        max_chars=VERIFY_SOURCE_MAX_CHARS,
    )
    verify_chars = (
        VERIFY_OVERHEAD_CHARS + len("\n".join(source)) if source is not None else None
    )
    levels: dict[str, dict] = {}
    for level in OFFERED_DEPTH_LEVELS:
        bundle = card_bundle_for(evidence, config.rationale, level)
        chars = generator.prompt_chars(
            evidence,
            symbol_context=context,
            bundle=bundle,
            level=level,
            target_path=target.path,
            **inputs,
        )
        undescribed = sum(1 for item in bundle.items if not item.commit.llm_description)
        chars += undescribed * median_description_chars
        entry: dict[str, Any] = {
            "input_tokens": round(chars / CHARS_PER_TOKEN),
            "undescribed": undescribed,
            "commits": len(bundle.items),
        }
        if verifies(level) and verify_chars is not None:
            entry["verify_input_tokens"] = round(verify_chars / CHARS_PER_TOKEN)
        levels[level] = entry
    return levels


# ---- pricing, at read time ---------------------------------------------------


def _ledger(project_id: int, task: str, *columns: Any) -> list[tuple]:
    """The newest usage rows of one project and task, for the given columns."""
    with get_session() as session:
        statement = (
            select(*columns)
            .where(UsageEvent.project_id == project_id)
            .where(UsageEvent.task == task)
            .order_by(UsageEvent.id.desc())
            .limit(DESCRIPTION_SAMPLE)
        )
        return [tuple(row) for row in session.exec(statement).all()]


def _median(values: list[int]) -> int | None:
    return round(statistics.median(values)) if values else None


def _median_description_chars() -> int:
    """The median length of the project's commit descriptions (the default when none)."""
    with project_session() as session:
        lengths = session.exec(
            select(func.length(Commit.llm_description))
            .where(col(Commit.llm_description).is_not(None))
            .limit(DESCRIPTION_SAMPLE)
        ).all()
    return _median([n for n in lengths if n]) or DEFAULT_DESCRIPTION_CHARS


def render_profile(
    raw: dict,
    config: Config,
    *,
    project_id: int,
    overrides: PriceOverrides,
    show_cost: bool,
    head: str | None,
) -> dict:
    """The route's answer for a measured sample: tokens, prices and the description lines.

    Parameters
    ----------
    raw : dict
        :func:`sample_profile`'s ``ready`` result.
    config : Config
        The project's resolved config (the rationale and analyze models).
    project_id : int
        The project, for its usage rows.
    overrides : PriceOverrides
        The org's price overrides.
    show_cost : bool
        ``False`` for a caller without ``project.usage``: tokens and every
        price are left out (tokens times a public price table give the
        cost back), ``cost_hidden`` is ``True``.
    head : str or None
        The ``HEAD`` the sample was measured at.

    Returns
    -------
    dict
        ``status`` ``"ready"``, ``head``, ``computed_at``, ``sampled``,
        ``model`` (``provider``, ``model``, ``source`` ``pinned`` /
        ``served`` / ``None``, ``price_known``), ``prices_as_of``,
        ``output_source`` (``ledger`` / ``default``), ``levels`` (per level:
        ``calls``, ``input_tokens`` ``{p50, p90}``, ``output_tokens``,
        ``cost`` ``{low, high}`` or ``None``, ``first_card`` likewise),
        ``descriptions`` (``undescribed_share``, ``per_commit_usd``) and
        ``cheaper_descriptions`` (or ``None``).
    """
    provider, pinned = config.cache_identity("rationale")
    served = _ledger(
        project_id, "rationale", UsageEvent.provider, UsageEvent.model_served
    )
    model, source = pinned, "pinned" if pinned else None
    if model is None:
        latest = next((row for row in served if row[1]), None)
        if latest is not None:
            provider, model, source = latest[0], latest[1], "served"
    custom = has_custom_endpoint(config, provider)
    priced = price_for(provider, model, overrides, custom_endpoint=custom)
    outputs = [
        r[0] for r in _ledger(project_id, "rationale", UsageEvent.output_tokens) if r[0]
    ]
    draft_out = _median(outputs)
    describe_in, describe_out = _describe_tokens(project_id, config)
    describe_price = _analyze_price(config, overrides)
    per_commit = (
        None
        if describe_price is None
        else float(
            cost(
                describe_price[0],
                SimpleNamespace(input_tokens=describe_in, output_tokens=describe_out),
            )
        )
    )

    targets = raw["targets"]
    levels: dict[str, dict] = {}
    for level in OFFERED_DEPTH_LEVELS:
        rows = [t[level] for t in targets if level in t]
        verify = verifies(level)
        # A level that verifies reads the target's source once more.
        inputs = [r["input_tokens"] + (r.get("verify_input_tokens") or 0) for r in rows]
        out = (draft_out or DEFAULT_DRAFT_OUTPUT_TOKENS) + (
            VERIFY_OUTPUT_TOKENS if verify else 0
        )
        body: dict[str, Any] = {
            "calls": 2 if verify else 1,
            "input_tokens": None,
            "output_tokens": None,
            "cost": None,
            "first_card": None,
        }
        if show_cost and inputs:
            body["input_tokens"] = {
                "p50": round(_percentile(inputs, 0.5)),
                "p90": round(_percentile(inputs, 0.9)),
            }
            body["output_tokens"] = out
            if priced is not None:
                costs = [
                    float(
                        cost(
                            priced[0],
                            SimpleNamespace(input_tokens=i, output_tokens=out),
                        )
                    )
                    for i in inputs
                ]
                body["cost"] = _range(costs)
                if per_commit is not None:
                    firsts = [
                        c + r["undescribed"] * per_commit for c, r in zip(costs, rows)
                    ]
                    body["first_card"] = _range(firsts)
        levels[level] = body

    medium = [t["medium"] for t in targets if "medium" in t]
    commits = sum(r["commits"] for r in medium)
    undescribed = sum(r["undescribed"] for r in medium)
    answer: dict[str, Any] = {
        "status": "ready",
        "head": head,
        "computed_at": raw["computed_at"],
        "sampled": raw["sampled"],
        "model": {
            "provider": provider,
            "model": model,
            "source": source,
            "price_known": priced is not None,
        },
        "prices_as_of": (
            None
            if priced is None
            else (
                "your organization's prices"
                if priced[1].startswith("org:")
                else BUNDLED_AS_OF
            )
        ),
        "output_source": "ledger" if draft_out else "default",
        "cost_hidden": not show_cost,
        "levels": levels,
        "descriptions": {
            "undescribed_share": round(undescribed / commits, 3) if commits else 0.0,
            "per_commit_usd": round(per_commit, 4)
            if show_cost and per_commit is not None
            else None,
        },
        "cheaper_descriptions": (
            _cheaper(config, overrides, describe_in, describe_out, per_commit)
            if show_cost
            else None
        ),
    }
    return answer


def _range(values: list[float]) -> dict[str, float]:
    """The p50-p90 range of ``values`` in dollars."""
    return {
        "low": round(_percentile(values, 0.5), 4),
        "high": round(_percentile(values, 0.9), 4),
    }


def _describe_tokens(project_id: int, config: Config) -> tuple[int, int]:
    """One description call's median input and output tokens: this project's, else the first-scan arithmetic's."""
    rows = _ledger(
        project_id, "analyze", UsageEvent.input_tokens, UsageEvent.output_tokens
    )
    inputs = [r[0] for r in rows if r[0]]
    outputs = [r[1] for r in rows if r[1]]
    if inputs and outputs:
        return _median(inputs) or 0, _median(outputs) or 0
    try:
        model = config.model_for("analyze").model
    except ConfigError:
        model = None
    typical = estimate_tokens(
        [CommitSize(3, (20, 20, 20))],
        max_diff_chars=config.analyze.max_diff_chars,
        large_commit_file_count=config.analyze.large_commit_file_count,
        profile=model_profile(model),
    )
    return typical.input_tokens, typical.output_tokens


def _analyze_price(config: Config, overrides: PriceOverrides):
    """The analyze (description) model's price, or ``None``."""
    try:
        choice = config.model_for("analyze")
    except ConfigError:
        return None
    return price_for(
        choice.provider,
        choice.model,
        overrides,
        custom_endpoint=has_custom_endpoint(config, choice.provider),
    )


def _cheaper(
    config: Config,
    overrides: PriceOverrides,
    describe_in: int,
    describe_out: int,
    current: float | None,
) -> dict | None:
    """What a description would cost on a cheaper model this project already configures (R38)."""
    if current is None:
        return None
    best: tuple[float, str, str | None] | None = None
    for task in ("rationale", "chat"):
        try:
            choice = config.model_for(task)
        except ConfigError:
            continue
        hit = price_for(
            choice.provider,
            choice.model,
            overrides,
            custom_endpoint=has_custom_endpoint(config, choice.provider),
        )
        if hit is None:
            continue
        usd = float(
            cost(
                hit[0],
                SimpleNamespace(input_tokens=describe_in, output_tokens=describe_out),
            )
        )
        if usd < current and (best is None or usd < best[0]):
            best = (usd, choice.provider, choice.model)
    if best is None:
        return None
    return {
        "provider": best[1],
        "model": best[2],
        "per_commit_usd": round(best[0], 4),
        "current_per_commit_usd": round(current, 4),
    }


def run_sample(
    entry_store: DepthProfiles,
    project_id: int,
    entry: _Entry,
    root: Path,
    config: Config,
    *,
    git_budget: int,
) -> None:
    """Compute one project's sample and record the outcome (the thread body).

    Never raises: a failure is recorded on the entry for the next ask.
    """
    try:
        raw = sample_profile(
            root,
            config,
            git_budget=git_budget,
            median_description_chars=_median_description_chars(),
        )
    except Exception as exc:  # noqa: BLE001 -- a background job must not die silently
        _log.warning("depth estimate of project %s failed", project_id, exc_info=True)
        entry_store.finish(project_id, entry, None, str(exc) or exc.__class__.__name__)
    else:
        entry_store.finish(project_id, entry, raw, None)


__all__ = [
    "DepthProfiles",
    "render_profile",
    "run_sample",
    "sample_profile",
]
