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

from .bootstrap import DEFAULT_CODEGRAPH_IMAGE, ensure_codegraph_db
from .context import SymbolContext
from .exceptions import CodeGraphBootstrapError, CodeGraphError
from .graph import CodeGraph
from .paths import CODEGRAPH_DB_RELPATH
from .relation import Relation
from .symbol import Symbol

__all__ = [
    "CODEGRAPH_DB_RELPATH",
    "CodeGraph",
    "CodeGraphBootstrapError",
    "CodeGraphError",
    "DEFAULT_CODEGRAPH_IMAGE",
    "Relation",
    "Symbol",
    "SymbolContext",
    "ensure_codegraph_db",
]
