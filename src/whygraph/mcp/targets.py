"""Target-shaping utilities for WhyGraph's MCP feature modules.

A "target" is a resolved chunk of code — a file path plus a 1-based
inclusive line range, optionally tagged with the dotted symbol name it
came from. Every MCP tool operates on one: ``whygraph_evidence_for``
joins blame to PRs/issues for a target, ``whygraph_rationale_brief``
synthesises a card for one.

This module owns:

- :class:`Target` — the dataclass itself.
- :func:`resolve_target` — the central input validator that funnels both
  targeting modes (``qualified_name`` vs ``(path, line_start, line_end)``)
  into a single :class:`Target`.
- :func:`target_dict` — the JSON-payload serializer.
- :func:`repo_root` — repository-root resolution, kept here because
  :func:`resolve_target` calls it to feed CodeGraph lookups and the
  feature modules call it for the same reason.

Imports :mod:`whygraph.mcp.errors` for :class:`WhyGraphError`; imports no
feature module, so it never closes an import cycle.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from whygraph.core import _resolve_root, get_config
from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.services.codegraph import (
    CODEGRAPH_DB_RELPATH,
    CodeGraph,
    CodeGraphError,
    Symbol,
    refresh_codegraph_index,
)
from whygraph.services.git.credentials import pass_through_env

from .errors import WhyGraphError

_log = logging.getLogger(__name__)

RESYNC_TIMEOUT_SEC = 30.0
"""Kill a read-path ``codegraph sync`` after this long (the tool answers stale)."""

RESYNC_LOCK_WAIT_SEC = 5.0
"""How long a tool call waits for another call's re-sync of the same repo."""

_RESYNC_LOCKS: dict[Path, threading.Lock] = {}
_RESYNC_LOCKS_GUARD = threading.Lock()


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
    index_stale : bool
        ``True`` when the symbol's file changed since CodeGraph indexed it
        and the re-sync failed, so the line range may have drifted.
        Always ``False`` for an explicit path/line range.
    """

    path: str
    line_start: int
    line_end: int
    qualified_name: str | None
    index_stale: bool = False


def target_dict(target: Target) -> dict:
    """Serialize a :class:`Target` to a JSON-ready dict for a tool result.

    ``index_stale: true`` is added only when the range may have drifted
    (see :attr:`Target.index_stale`); a fresh target has no such key.
    """
    payload = {
        "path": target.path,
        "line_start": target.line_start,
        "line_end": target.line_end,
        "qualified_name": target.qualified_name,
    }
    if target.index_stale:
        payload["index_stale"] = True
    return payload


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

    Notes
    -----
    A ``qualified_name`` lookup first checks that the symbol's file still
    has the content CodeGraph indexed (sha256 vs ``files.content_hash``).
    If it changed, the index is re-synced (``codegraph sync``, bounded -
    see :func:`_resync_and_lookup`) and the symbol looked up again, so an
    edit that shifted its lines does not make blame read the wrong ones.
    When the re-sync cannot run, fails, or leaves the file stale, the
    target is marked :attr:`Target.index_stale`.
    """
    if qualified_name:
        if path or line_start or line_end:
            raise WhyGraphError(
                "pass either qualified_name OR (path, line_start, line_end), not both"
            )
        root = repo_root()
        codegraph_db = get_config().codegraph_db
        try:
            with CodeGraph.for_repository(root, codegraph_db=codegraph_db) as graph:
                symbol = graph.symbol(qualified_name)
                stale = symbol is not None and _is_stale(graph, root, symbol)
        except CodeGraphError as exc:
            raise WhyGraphError.wrap("qualified_name targeting needs CodeGraph", exc)
        if symbol is None:
            raise WhyGraphError(
                f"qualified_name {qualified_name!r} not found in CodeGraph"
            )
        index_stale = False
        if stale:
            resynced, fresh = _resync_and_lookup(root, codegraph_db, qualified_name)
            if resynced is not None:
                symbol = resynced
            index_stale = not fresh
        return Target(
            path=symbol.file_path,
            line_start=symbol.start_line,
            line_end=symbol.end_line,
            qualified_name=qualified_name,
            index_stale=index_stale,
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


def _is_stale(graph: CodeGraph, root: Path, symbol: Symbol) -> bool:
    """Whether ``symbol``'s file changed since CodeGraph indexed it.

    Unknown counts as fresh: an index without ``content_hash``, or a path
    :func:`~whygraph.core.safe_paths.check_inside` refuses (never read
    through a symlink). A file that is gone or unreadable counts as stale.
    """
    recorded = graph.file_hash(symbol.file_path)
    if recorded is None:
        return False
    try:
        file = check_inside(root, symbol.file_path)
    except UnsafePathError:
        return False
    try:
        current = hashlib.sha256(file.read_bytes()).hexdigest()
    except OSError:
        return True
    return current != recorded


def _resync_lock(root: Path) -> threading.Lock:
    with _RESYNC_LOCKS_GUARD:
        return _RESYNC_LOCKS.setdefault(root.resolve(), threading.Lock())


def _resync_and_lookup(
    root: Path, codegraph_db: Path | None, qualified_name: str
) -> tuple[Symbol | None, bool]:
    """Re-sync CodeGraph for ``root`` and look ``qualified_name`` up again.

    Bounded, because it runs inside a tool call: only with a local
    ``codegraph`` binary (never the Docker fallback), only an incremental
    ``sync`` (never a full rebuild), under :data:`RESYNC_TIMEOUT_SEC`, with
    the child-env allowlist, and never through a symlinked ``.codegraph/``.
    One re-sync per repository at a time; a caller that waited (at most
    :data:`RESYNC_LOCK_WAIT_SEC`) re-checks first, so concurrent tool calls
    after one edit share a single sync. It never fails the tool call.

    Returns
    -------
    tuple[Symbol or None, bool]
        The symbol as the index now has it (``None``: keep the old one), and
        whether its file now matches the index (``False``: still stale).
    """
    if shutil.which("codegraph") is None:
        return None, False
    try:
        check_inside(root, CODEGRAPH_DB_RELPATH)
    except UnsafePathError:
        return None, False
    lock = _resync_lock(root)
    if not lock.acquire(timeout=RESYNC_LOCK_WAIT_SEC):
        return None, False
    try:
        with CodeGraph.for_repository(root, codegraph_db=codegraph_db) as graph:
            symbol = graph.symbol(qualified_name)
            if symbol is not None and not _is_stale(graph, root, symbol):
                return symbol, True
        refresh_codegraph_index(
            root,
            capture=True,
            allow_rebuild=False,
            timeout=RESYNC_TIMEOUT_SEC,
            env=pass_through_env(),
        )
        with CodeGraph.for_repository(root, codegraph_db=codegraph_db) as graph:
            symbol = graph.symbol(qualified_name)
            # A sync that exits 0 without re-indexing the file leaves it stale.
            return symbol, symbol is not None and not _is_stale(graph, root, symbol)
    except CodeGraphError as exc:  # includes CodeGraphBootstrapError
        _log.warning(
            "CodeGraph re-sync for %s failed; using the indexed range: %s",
            qualified_name,
            exc,
        )
        return None, False
    finally:
        lock.release()
