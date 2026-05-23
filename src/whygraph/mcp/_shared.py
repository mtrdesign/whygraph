"""Helpers shared across WhyGraph's MCP feature modules.

Holds the package's error type, the code-chunk target abstraction, and
repository-root resolution. Feature modules (:mod:`whygraph.mcp.evidence`,
:mod:`whygraph.mcp.rationale`) import from here; this module imports no
feature module and no ``server`` module, so it never closes an import
cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from whygraph.core import _resolve_root, get_config
from whygraph.services.codegraph import CodeGraph, CodeGraphError


class WhyGraphError(RuntimeError):
    """Raised by an MCP tool when a request cannot be served.

    Surfaces to the agent as the tool's error message — phrased for a
    reader who will act on it (e.g. "run ``whygraph scan`` first").
    """


@dataclass(frozen=True, slots=True)
class Target:
    """A resolved code chunk an MCP tool operates on.

    Attributes
    ----------
    path : str
        File path, relative to the repository root.
    line_start : int
        First line of the chunk (1-based, inclusive).
    line_end : int
        Last line of the chunk (1-based, inclusive).
    qualified_name : str or None
        The dotted symbol name, when the target was resolved from one via
        CodeGraph. ``None`` when the caller passed a path/line range
        directly.
    """

    path: str
    line_start: int
    line_end: int
    qualified_name: str | None


def target_dict(target: Target) -> dict:
    """Serialize a :class:`Target` to a JSON-ready dict for a tool result."""
    return {
        "path": target.path,
        "line_start": target.line_start,
        "line_end": target.line_end,
        "qualified_name": target.qualified_name,
    }


def repo_root() -> Path:
    """Return the repository root the MCP server is operating on.

    Reuses :func:`whygraph.core._resolve_root` — the same project-root
    resolution the rest of the package uses — so the MCP layer does not
    grow a competing notion of "where is the repo".
    """
    return _resolve_root()


def resolve_target(
    *,
    path: str | None,
    line_start: int | None,
    line_end: int | None,
    qualified_name: str | None,
) -> Target:
    """Validate a tool's targeting arguments into a :class:`Target`.

    Exactly one targeting mode must be supplied: a ``qualified_name`` (a
    dotted symbol name, resolved to a file/line range via CodeGraph), or
    the explicit ``(path, line_start, line_end)`` triple.

    Parameters
    ----------
    path, line_start, line_end : str, int, int or None
        The explicit-range targeting mode. All three required together.
    qualified_name : str or None
        The symbol-name targeting mode.

    Returns
    -------
    Target
        The resolved chunk.

    Raises
    ------
    WhyGraphError
        If both modes or neither mode is supplied, if the line range is
        invalid, or if CodeGraph cannot resolve ``qualified_name``.
    """
    if qualified_name:
        if path or line_start or line_end:
            raise WhyGraphError(
                "pass either qualified_name OR (path, line_start, line_end), "
                "not both"
            )
        try:
            with CodeGraph.for_repository(
                repo_root(), codegraph_db=get_config().codegraph_db
            ) as graph:
                symbol = graph.symbol(qualified_name)
        except CodeGraphError as exc:
            raise WhyGraphError(
                f"qualified_name targeting needs CodeGraph: {exc}"
            ) from exc
        if symbol is None:
            raise WhyGraphError(
                f"qualified_name {qualified_name!r} not found in CodeGraph"
            )
        return Target(
            path=symbol.file_path,
            line_start=symbol.start_line,
            line_end=symbol.end_line,
            qualified_name=qualified_name,
        )

    if not (path and line_start and line_end):
        raise WhyGraphError(
            "pass either qualified_name OR all of (path, line_start, line_end)"
        )
    if line_start < 1 or line_end < line_start:
        raise WhyGraphError("line_start must be >= 1 and line_end >= line_start")
    return Target(
        path=path,
        line_start=line_start,
        line_end=line_end,
        qualified_name=None,
    )
