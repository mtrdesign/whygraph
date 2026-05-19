"""Schema scaffolding shared by every WhyGraph SQLModel table.

Two pieces live here:

* :data:`metadata` — the :class:`sqlmodel.SQLModel` registry that Alembic's
  ``env.py`` imports as ``target_metadata``.
* :class:`WhygraphTable` — base class every table model inherits from. It
  derives ``__tablename__`` automatically from the class name (CamelCase
  → snake_case), so models can be defined without per-class string
  overrides (and without the Pyright/Pylance churn that SQLAlchemy 2.x's
  ``declared_attr`` typing causes when overriding with plain strings).

Conversion examples
-------------------
``Author``       → ``author``
``Commit``       → ``commit``
``Issue``        → ``issue``
``PullRequest``  → ``pull_request``
``PRIssueLink``  → ``pr_issue_link``
``RationaleCache`` → ``rationale_cache``
``ScanState``    → ``scan_state``
"""

from __future__ import annotations

import re

from sqlalchemy.orm import declared_attr
from sqlmodel import SQLModel

metadata = SQLModel.metadata

_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def _camel_to_snake(name: str) -> str:
    """Convert a ``CamelCase`` identifier to ``snake_case``.

    Handles acronym runs the way the wider Python community does:
    ``PRIssueLink`` becomes ``pr_issue_link``, not ``p_r_issue_link``.
    """
    step1 = _CAMEL_BOUNDARY_1.sub(r"\1_\2", name)
    return _CAMEL_BOUNDARY_2.sub(r"\1_\2", step1).lower()


class WhygraphTable(SQLModel):
    """Base class for every WhyGraph SQLModel table.

    Provides an auto-derived ``__tablename__`` so concrete models don't
    need to spell out the table name (and don't fight the SQLAlchemy
    declarative type stubs). Subclasses must still pass ``table=True``
    to mark themselves as concrete tables, e.g.::

        class Author(WhygraphTable, table=True):
            id: int | None = Field(default=None, primary_key=True)
            ...

    Notes
    -----
    ``WhygraphTable`` itself has *no* ``table=True`` — it stays an
    abstract base and is not mapped to any SQLite table.
    """

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805 -- declared_attr passes the class
        return _camel_to_snake(cls.__name__)


__all__ = ["WhygraphTable", "metadata"]
