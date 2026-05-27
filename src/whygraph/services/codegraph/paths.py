"""Filesystem layout constants for the codegraph service.

Holds the project-relative path to CodeGraph's SQLite database in one
place so the read path (:mod:`whygraph.services.codegraph.graph`) and the
write path (:mod:`whygraph.services.codegraph.bootstrap`) can't drift
apart.

Attributes
----------
CODEGRAPH_DB_RELPATH : Path
    Path of CodeGraph's SQLite database relative to a repository root.
    Currently ``.codegraph/codegraph.db`` — the layout CodeGraph itself
    writes from ``codegraph init``.
"""

from __future__ import annotations

from pathlib import Path

CODEGRAPH_DB_RELPATH: Path = Path(".codegraph") / "codegraph.db"

__all__ = ["CODEGRAPH_DB_RELPATH"]
