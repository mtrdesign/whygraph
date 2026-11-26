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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from whygraph.core import get_config
from whygraph.core.config import DEPTH_LEVELS, OFFERED_DEPTH_LEVELS, RationaleConfig

from .plans import _innermost
from .targets import repo_root

_log = logging.getLogger(__name__)

AUTO = "auto"
"""The project depth that lets :func:`resolve_depth` choose per call."""

INTENTS: tuple[str, ...] = ("explain", "edit", "refactor", "delete", "review", "plan")
"""The tasks a caller may name (``intent``)."""

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
