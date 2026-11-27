"""Which depth a rationale card is built at: the ``auto`` resolver and its signals.

Plan 62 section 14.3. :func:`resolve_depth` is one pure function, run
before the cache lookup; it follows a five-step table:

=====================  =========================================================
Step                   Rule
=====================  =========================================================
1 Fixed                An explicit ``depth``, or a project depth other than
                       ``auto``, is used as is; only step 5 applies.
2 Task base (auto)     ``explain`` / ``review`` -> ``low``; ``plan`` / ``edit``
                       -> ``medium``; ``refactor`` / ``delete`` -> ``xhigh``
                       when the symbol has callers (``high`` until Part D
                       ships), ``high`` when it has none or the count is
                       unknown; no intent -> ``medium``. ``review`` accepts any
                       cached depth and skips steps 3-4.
3 Importance (auto)    +1 at :data:`IMPORTANT_CALLERS` distinct callers or
                       more; an unknown count changes nothing.
4 Budget (auto)        -1 when a covering budget is at 75 % or more
                       (:func:`whygraph.core.usage.usage_pressure`).
5 Ceiling              Clamp to ``low`` .. the effective ceiling after each
                       step; a level that is not offered yet maps to the
                       highest offered one.
=====================  =========================================================

The signal it needs from the index, :func:`distinct_callers`, is not pure:
it reads the bound project's CodeGraph index and never raises.
:func:`resolve_for_target` gathers the signals for one target and calls
:func:`resolve_depth`; :func:`depth_block` and :func:`deeper` shape the
card's ``depth`` and ``deeper`` (plan 62 section 14.4).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from whygraph.analyze.card_bundle import LINE_LEVEL_SOURCES, level_caps, verifies
from whygraph.core import get_config
from whygraph.core.config import DEPTH_LEVELS, OFFERED_DEPTH_LEVELS, RationaleConfig
from whygraph.core.usage import usage_pressure

from .plans import _innermost
from .targets import repo_root

if TYPE_CHECKING:
    from whygraph.analyze import CommitEvidence

    from .targets import Target

_log = logging.getLogger(__name__)

AUTO = "auto"
"""The project depth that lets :func:`resolve_depth` choose per call."""

Intent = Literal["explain", "edit", "refactor", "delete", "review", "plan"]
"""The ``intent`` input of the tool and of ``/api/v1`` (``api_v1.Intent``)."""

INTENTS: tuple[str, ...] = get_args(Intent)
"""The tasks a caller may name (``intent``)."""

OfferedDepth = Literal[OFFERED_DEPTH_LEVELS]  # type: ignore[valid-type]
"""The ``depth`` input a caller may pass: the offered levels only.

Built from :data:`~whygraph.core.config.OFFERED_DEPTH_LEVELS`, so input
validation refuses ``xhigh`` until Part D ships (plan 62 section 14.1) and
follows that tuple when it does.
"""

IMPORTANT_CALLERS = 5
"""Distinct callers at which a symbol counts as important (one level up)."""

_TASK_BASE: dict[str | None, str] = {
    "explain": "low",
    "review": "low",
    "plan": "medium",
    "edit": "medium",
    None: "medium",
}
"""Step 2's base level per intent (``refactor`` / ``delete`` depend on callers)."""

_LABELS: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
}


@dataclass(frozen=True)
class DepthResolution:
    """The depth a card is built at, and why.

    Attributes
    ----------
    requested : str
        What the caller or project asked for: an explicit ``depth``, else
        the project's ``[rationale].depth`` (``"auto"`` or a level).
    resolved : str
        The level to build (one of
        :data:`~whygraph.core.config.OFFERED_DEPTH_LEVELS`), never above
        the effective ceiling.
    reason : str
        A short sentence for the card ("Auto chose Medium because the task
        was \\"edit\\".").
    any_cached_depth : bool
        ``True`` for ``intent="review"`` under ``auto``: a cached card of
        any depth satisfies the call.
    cached_only : bool
        The caller's ``cached_only``, or, when it passed ``None``, ``True``
        for ``intent="review"`` and ``False`` otherwise.
    """

    requested: str
    resolved: str
    reason: str
    any_cached_depth: bool = False
    cached_only: bool = False

    def as_dict(self) -> dict[str, str]:
        """The card's ``depth`` block: ``{requested, resolved, reason}``.

        Returns
        -------
        dict[str, str]
            JSON-ready.
        """
        return {
            "requested": self.requested,
            "resolved": self.resolved,
            "reason": self.reason,
        }


def effective_ceiling(rationale: RationaleConfig) -> str:
    """The lower of the project's ``max_depth`` and the org's ``org_max_depth``.

    Parameters
    ----------
    rationale : RationaleConfig
        The merged ``[rationale]`` section.

    Returns
    -------
    str
        A level name.
    """
    return min(rationale.max_depth, rationale.org_max_depth, key=DEPTH_LEVELS.index)


def _check_level(name: str, value: str) -> None:
    """Raise ``ValueError`` unless ``value`` is a depth level."""
    if value not in DEPTH_LEVELS:
        raise ValueError(f"unknown {name} {value!r}; expected one of {DEPTH_LEVELS}")


def _join(parts: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


class _Clamp:
    """Step 5: clamp a level index to ``low`` .. the effective ceiling.

    A level above the highest offered one (``xhigh`` until Part D ships)
    maps to it; :attr:`note` says what capped a level, if anything did.
    """

    def __init__(self, ceiling: str) -> None:
        offered_top = DEPTH_LEVELS.index(OFFERED_DEPTH_LEVELS[-1])
        ceiling_index = DEPTH_LEVELS.index(ceiling)
        self.top = min(ceiling_index, offered_top)
        self._by_ceiling = ceiling_index < offered_top
        self.capped = False

    def __call__(self, index: int) -> int:
        if index > self.top:
            self.capped = True
            return self.top
        return max(index, 0)

    @property
    def note(self) -> str | None:
        if not self.capped:
            return None
        label = _LABELS[DEPTH_LEVELS[self.top]]
        if self._by_ceiling:
            return f"Capped at {label} by the depth ceiling."
        return f"Capped at {label}: {_LABELS['xhigh']} is not offered yet."


def resolve_depth(
    *,
    depth: str | None,
    project_depth: str,
    intent: str | None,
    ceiling: str,
    callers: int | None,
    pressure: str | None = None,
    cached_only: bool | None = None,
    thin_history: bool = False,
) -> DepthResolution:
    """Resolve the depth of one card (plan 62 section 14.3).

    Parameters
    ----------
    depth : str or None
        The caller's explicit level, or ``None``. Capped at ``ceiling``,
        never refused; budget pressure does not lower it (R37).
    project_depth : str
        The project's ``[rationale].depth``: ``"auto"`` or a level.
    intent : str or None
        One of :data:`INTENTS`, or ``None`` (no task named).
    ceiling : str
        The effective ceiling (:func:`effective_ceiling`).
    callers : int or None
        Distinct callers of the symbol covering the target
        (:func:`distinct_callers`); ``None`` when unknown. Read only under
        ``auto``.
    pressure : str or None, optional
        The budget scope at 75 % or more
        (:func:`whygraph.core.usage.usage_pressure`), or ``None``. Read only
        under ``auto``.
    cached_only : bool or None, optional
        The caller's ``cached_only``; ``None`` means ``True`` for
        ``intent="review"`` and ``False`` otherwise.
    thin_history : bool, optional
        Whether the target's history is thin (one commit, no PR); only
        mentioned in the reason.

    Returns
    -------
    DepthResolution
        The resolved level and why.

    Raises
    ------
    ValueError
        For an unknown level or intent (a programming error: the tool and
        the API validate both before they get here).
    """
    if depth is not None:
        _check_level("depth", depth)
    if project_depth != AUTO:
        _check_level("project depth", project_depth)
    _check_level("ceiling", ceiling)
    if intent is not None and intent not in INTENTS:
        raise ValueError(f"unknown intent {intent!r}; expected one of {INTENTS}")
    review = intent == "review"
    only = review if cached_only is None else cached_only
    clamp = _Clamp(ceiling)
    thin = ["History is thin (one commit, no PR)."] if thin_history else []

    fixed = depth if depth is not None else project_depth
    if fixed != AUTO:  # step 1, then step 5
        level = clamp(DEPTH_LEVELS.index(fixed))
        who = "was asked for" if depth is not None else "is the project depth"
        sentences = [f"{_LABELS[fixed]} {who}.", clamp.note, *thin]
        return DepthResolution(
            requested=fixed,
            resolved=DEPTH_LEVELS[level],
            reason=" ".join(s for s in sentences if s),
            cached_only=only,
        )

    # Step 2: the task base.
    parts = ["no task was given" if intent is None else f'the task was "{intent}"']
    if intent in ("refactor", "delete"):
        base = "xhigh" if callers else "high"
        if callers is not None and callers < IMPORTANT_CALLERS:  # else step 3 says
            parts.append(
                "the symbol has callers" if callers else "the symbol has no callers"
            )
    else:
        base = _TASK_BASE[intent]
    level = clamp(DEPTH_LEVELS.index(base))
    unknown = None
    if not review:
        # Step 3: importance.
        if callers is None:
            unknown = "The caller count is unknown, so importance did not count."
        elif callers >= IMPORTANT_CALLERS:
            parts.append(f"the symbol has {callers} distinct callers")
            level = clamp(level + 1)
        # Step 4: budget pressure.
        if pressure is not None:
            parts.append(f"the {pressure} budget is at 75 % or more")
            level = clamp(level - 1)
    resolved = DEPTH_LEVELS[level]
    sentences = [
        f"Auto chose {_LABELS[resolved]} because {_join(parts)}.",
        clamp.note,
        "A cached card of any depth is accepted." if review else None,
        unknown,
        *thin,
    ]
    return DepthResolution(
        requested=AUTO,
        resolved=resolved,
        reason=" ".join(s for s in sentences if s),
        any_cached_depth=review,
        cached_only=only,
    )


def distinct_callers(path: str, line_start: int, line_end: int) -> int | None:
    """Distinct callers of the innermost indexed symbol covering a line range.

    Reads the bound project's CodeGraph index (the context seam:
    :func:`~whygraph.mcp.targets.repo_root` and ``codegraph_db``).
    :meth:`~whygraph.services.codegraph.CodeGraph.callers` returns one row
    per ``calls`` edge, so callers are counted by distinct node id.

    Parameters
    ----------
    path : str
        Repo-relative file path.
    line_start, line_end : int
        The target's lines (1-based, inclusive).

    Returns
    -------
    int or None
        The count, or ``None`` when there is no index or no indexed symbol
        covers the range. Never raises.
    """
    from whygraph.services.codegraph import CodeGraph

    try:
        with CodeGraph.for_repository(
            repo_root(), codegraph_db=get_config().codegraph_db
        ) as graph:
            symbols = [
                s for s in graph.area(path) if s.kind != "file" and s.file_path == path
            ]
            symbol = _innermost(symbols, line_start, line_end)
            if symbol is None:
                return None
            return len({r.symbol.id for r in graph.callers(symbol.id)})
    except Exception:  # noqa: BLE001 - no index, a stale index: unknown
        _log.debug("rationale depth: no caller count for %s", path, exc_info=True)
        return None


def resolve_for_target(
    target: Target,
    evidence: Sequence[CommitEvidence],
    *,
    intent: str | None = None,
    depth: str | None = None,
    cached_only: bool | None = None,
    count_callers: bool = True,
) -> DepthResolution:
    """Resolve the depth of ``target``'s card from the bound project's config.

    Reads the merged ``[rationale]`` section (its ``depth`` and the
    effective ceiling) and, under ``auto`` for any intent but ``review``,
    the two signals: :func:`distinct_callers` and
    :func:`~whygraph.core.usage.usage_pressure`. Run before the cache
    lookup (plan 62 section 14.3).

    Parameters
    ----------
    target : Target
        The resolved target.
    evidence : Sequence[CommitEvidence]
        Its evidence; one commit with no PR is "thin history".
    intent : str or None, optional
        One of :data:`INTENTS`, or ``None``.
    depth : str or None, optional
        The caller's explicit level, or ``None``.
    cached_only : bool or None, optional
        The caller's ``cached_only`` (``None``: decided by ``intent``).
    count_callers : bool, optional
        ``False`` when ``target``'s lines are not this repository's (the
        platform's answer to a requester's working-tree range): the caller
        count is then unknown.

    Returns
    -------
    DepthResolution
        The resolved level and why.

    Raises
    ------
    ValueError
        For an unknown level or intent.
    """
    rationale = get_config().rationale
    signals = depth is None and rationale.depth == AUTO and intent != "review"
    callers = None
    pressure = None
    if signals:
        if count_callers:
            callers = distinct_callers(target.path, target.line_start, target.line_end)
        pressure = usage_pressure()
    thin = len(evidence) == 1 and not evidence[0].pull_requests
    return resolve_depth(
        depth=depth,
        project_depth=rationale.depth,
        intent=intent,
        ceiling=effective_ceiling(rationale),
        callers=callers,
        pressure=pressure,
        cached_only=cached_only,
        thin_history=thin,
    )


SERVED_CACHED = "cached"
"""A cached card of another level was served (:func:`depth_block`)."""

SERVED_KEPT = "kept"
"""The card was rebuilt at a deeper row's level, never replacing it shallower."""

SERVED_UNVERIFIED = "unverified"
"""The verification pass did not complete, so the card was stored lower."""


def depth_block(
    resolution: DepthResolution, level: str, served: str = SERVED_CACHED
) -> dict[str, str]:
    """The card's ``depth``: ``{requested, resolved, reason}`` for the card served.

    ``resolved`` is the level of the card actually returned. When that is
    not the resolver's level - a deeper cached card satisfied the request,
    ``review`` accepted a shallower one, a deeper row was rebuilt at its
    own level, or a ``high`` draft was stored lower - ``resolved`` is the
    card's level and ``reason`` gains a sentence saying why.

    Parameters
    ----------
    resolution : DepthResolution
        What :func:`resolve_depth` chose.
    level : str
        The level of the card returned.
    served : str, optional
        Why the levels differ: :data:`SERVED_CACHED` (default),
        :data:`SERVED_KEPT` or :data:`SERVED_UNVERIFIED`.

    Returns
    -------
    dict[str, str]
        JSON-ready.
    """
    block = resolution.as_dict()
    if level == resolution.resolved:
        return block
    label = _LABELS[level]
    sentence = {
        SERVED_CACHED: f"A cached {label} card was served.",
        SERVED_KEPT: f"Built at {label} to keep the deeper card of these lines.",
        SERVED_UNVERIFIED: (
            f"Stored at {label}: the verification pass did not complete."
        ),
    }[served]
    block["resolved"] = level
    block["reason"] = f"{block['reason']} {sentence}"
    return block


def deeper(
    level: str, ceiling: str, evidence: Sequence[CommitEvidence]
) -> dict[str, str] | None:
    """What a card one level deeper would add for this target, or ``None``.

    The next level's differences from ``level`` (plan 62 section 14.1),
    kept to the ones this target's evidence would show: the verification
    pass, more of the file's other commits, the PRs of earlier commits of
    the lines, the draft commits of a squash-merged PR.

    Parameters
    ----------
    level : str
        The level of the card returned.
    ceiling : str
        The effective ceiling (:func:`effective_ceiling`).
    evidence : Sequence[CommitEvidence]
        The target's evidence.

    Returns
    -------
    dict[str, str] or None
        ``{"level", "adds"}`` (``adds`` one sentence); ``None`` at the
        ceiling (or the highest offered level), or when the next level
        would add nothing for this target.
    """
    top = min(DEPTH_LEVELS.index(ceiling), DEPTH_LEVELS.index(OFFERED_DEPTH_LEVELS[-1]))
    index = DEPTH_LEVELS.index(level)
    if index >= top:
        return None
    nxt = DEPTH_LEVELS[index + 1]
    here, there = level_caps(level), level_caps(nxt)
    parts: list[str] = []
    if verifies(nxt) and not verifies(level):
        parts.append("checks each constraint and risk against today's code")
    if nxt == "medium" and any(
        item.source in LINE_LEVEL_SOURCES
        and item.source not in ("blame", "pr-origin")
        and item.pull_requests
        for item in evidence
    ):
        parts.append("reads the PRs of the earlier commits that shaped these lines")
    context = sum(1 for item in evidence if item.source not in LINE_LEVEL_SOURCES)
    shown, more = (
        min(context, here.context_commits),
        min(context, there.context_commits),
    )
    if more > shown:
        of = f"of the {context} other commits to this file"
        parts.append(
            f"reads {more} {of}"
            if not shown
            else f"reads {more} instead of {shown} {of}"
        )
    if (
        there.feature_commits
        and not here.feature_commits
        and any(item.source == "pr-origin" for item in evidence)
    ):
        parts.append("shows the draft commits of squash-merged PRs")
    if not parts:
        return None
    text = _join(parts)
    return {"level": nxt, "adds": f"{text[0].upper()}{text[1:]}."}
