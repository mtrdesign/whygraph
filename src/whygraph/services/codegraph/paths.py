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
GUARD_STAMP_RELPATH : Path
    WhyGraph's stamp beside the index, written once an index was built from
    scratch behind the symbolic-link guard (see
    :func:`whygraph.services.codegraph.bootstrap.index_predates_guard`).
"""

from __future__ import annotations

from pathlib import Path

CODEGRAPH_DB_RELPATH: Path = Path(".codegraph") / "codegraph.db"
GUARD_STAMP_RELPATH: Path = Path(".codegraph") / "whygraph-guard"

__all__ = ["CODEGRAPH_DB_RELPATH", "GUARD_STAMP_RELPATH"]
