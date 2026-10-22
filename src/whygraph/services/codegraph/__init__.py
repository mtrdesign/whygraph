"""CodeGraph service: read-only structural queries over the code graph.

WhyGraph reads `CodeGraph <https://github.com/colbymchenry/codegraph>`_'s
SQLite index directly to answer structural questions — who calls a symbol,
what it calls, where it is defined. There is no subprocess and no MCP
roundtrip; the service is a thin, typed query layer over the ``nodes`` and
``edges`` tables.

Public API
----------
* :class:`CodeGraph` — the entry point: opens ``.codegraph/codegraph.db``
  read-only and answers symbol look-ups, substring search, caller/callee
  traversal, and :class:`SymbolContext` assembly.
* :class:`Symbol` — value object for one graph node (a function, class, …).
* :class:`Relation` — value object for one ``calls`` / ``contains`` /
  ``imports`` edge, resolved to the neighbouring :class:`Symbol`.
* :class:`SymbolContext` — a symbol bundled with its callers and callees; the
  structural-evidence unit the rationale generator consumes.
* :class:`CodeGraphError` — raised when the database is missing, or a query
  fails.

Examples
--------
>>> from pathlib import Path
>>> with CodeGraph.for_repository(Path.cwd()) as graph:   # doctest: +SKIP
...     ctx = graph.context("whygraph.cli.main")
...     if ctx:
...         print(len(ctx.callers), "callers")
"""

from .bootstrap import (
    DEFAULT_CODEGRAPH_IMAGE,
    ensure_codegraph_db,
    hosted_env,
    index_predates_guard,
    refresh_codegraph_index,
)
from .context import SymbolContext
from .exceptions import (
    CodeGraphBootstrapError,
    CodeGraphError,
    CodeGraphUnsafeSymlinkError,
)
from .graph import CodeGraph
from .paths import CODEGRAPH_DB_RELPATH, GUARD_STAMP_RELPATH
from .relation import Relation
from .symbol import Symbol
from .symlinks import escaping_symlinks
from .writer_lock import (
    async_writer_lock,
    configure_lock_dir,
    writer_lock,
)

__all__ = [
    "CODEGRAPH_DB_RELPATH",
    "CodeGraph",
    "CodeGraphBootstrapError",
    "CodeGraphError",
    "CodeGraphUnsafeSymlinkError",
    "DEFAULT_CODEGRAPH_IMAGE",
    "GUARD_STAMP_RELPATH",
    "Relation",
    "Symbol",
    "SymbolContext",
    "async_writer_lock",
    "configure_lock_dir",
    "ensure_codegraph_db",
    "escaping_symlinks",
    "hosted_env",
    "index_predates_guard",
    "refresh_codegraph_index",
    "writer_lock",
]
