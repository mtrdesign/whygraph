"""SQLModel tables of the scan's core pass (plan 49 section 6).

Five tables, all written by :class:`~whygraph.scan.core_crawler.CoreCrawler`
and all recomputed by the next scan (none travels in a transfer bundle):

* :class:`CoreState` - one row, the pass's bookkeeping.
* :class:`CoreFile` - one row per blamed file, a cache that outlives the
  file's selection.
* :class:`CoreRename` - one row per blamed rename predecessor.
* :class:`CoreCommit` - the core set, replaced only by a complete pass.
* :class:`CoreBulkPair` - the ``(bulk commit, core file)`` slices of the
  core files, replaced only by a complete pass.

They share one module, not one each, because they are one feature's
state and are only ever read together.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlmodel import Field

from whygraph.db.base import WhygraphTable


class CoreState(WhygraphTable, table=True):
    """The core pass's bookkeeping: one row, ``id = 1``.

    Attributes
    ----------
    id : int
        Always ``1``. Primary key.
    rules_key : str
        sha256 of the validated ``.git-blame-ignore-revs`` entries and the
        rules version; a change re-blames every file and rename.
    selection : str or None
        ``"codegraph"`` (ranked from the code index) or ``"all_text"`` (the
        fallback); written only by a complete pass.
    fallback_reason : str or None
        ``"no_index"`` when the fallback was taken because no index file
        existed, ``"unusable"`` for any other fallback, ``None`` with
        ``selection = "codegraph"``; written only by a complete pass.
    files_selected : int or None
        Core files chosen by the last complete pass.
    files_total : int
        Selected files plus their current rename events, for "N of M".
    files_done : int
        How many of those have a current row.
    completed_at : str or None
        ISO timestamp of the last complete pass; ``None`` until one.
    completed_head : str or None
        ``HEAD`` of the last complete pass.
    """

    id: int = Field(primary_key=True)
    rules_key: str = Field(sa_type=Text)
    selection: str | None = Field(default=None, sa_type=Text)
    fallback_reason: str | None = Field(default=None, sa_type=Text)
    files_selected: int | None = Field(default=None)
    files_total: int = Field(default=0)
    files_done: int = Field(default=0)
    completed_at: str | None = Field(default=None, sa_type=Text)
    completed_head: str | None = Field(default=None, sa_type=Text)


class CoreFile(WhygraphTable, table=True):
    """The whole-file blame of one file at ``HEAD``.

    Kept for every live path once blamed, selected or not, so a file that
    re-enters the selection costs nothing.

    Attributes
    ----------
    path : str
        The path at ``HEAD``. Primary key.
    blob_sha : str
        The blob the row was computed from.
    computed_head : str
        The ``HEAD`` this row was last checked at.
    status : str
        ``"ok"``, ``"skipped"`` (missing, binary, over a cap) or
        ``"failed"`` (blame raised; not retried until the blob, history or
        rules change).
    shas : str
        Space-joined SHAs of the commits owning a line; empty unless ok.
    """

    path: str = Field(primary_key=True, sa_type=Text)
    blob_sha: str = Field(sa_type=Text)
    computed_head: str = Field(sa_type=Text)
    status: str = Field(sa_type=Text)
    shas: str = Field(sa_type=Text)


class CoreRename(WhygraphTable, table=True):
    """The whole-predecessor blame of one rename event.

    Immutable: it depends only on its key and the rules.

    Attributes
    ----------
    rename_sha : str
        The commit that renamed the file. Primary key (with ``pred_path``).
    pred_path : str
        The path before the rename. Primary key (with ``rename_sha``).
    status : str
        ``"ok"``, ``"skipped"`` or ``"failed"``, as :attr:`CoreFile.status`.
    shas : str
        Space-joined SHAs of the commits owning a line of the predecessor
        at the rename's first parent; empty unless ok.
    """

    rename_sha: str = Field(primary_key=True, sa_type=Text)
    pred_path: str = Field(primary_key=True, sa_type=Text)
    status: str = Field(sa_type=Text)
    shas: str = Field(sa_type=Text)


class CoreCommit(WhygraphTable, table=True):
    """One commit of the core set; the table is replaced only by a complete pass.

    Attributes
    ----------
    sha : str
        The commit. Primary key.
    """

    sha: str = Field(primary_key=True, sa_type=Text)


class CoreBulkPair(WhygraphTable, table=True):
    """One per-file slice of a bulk commit in a core file (D14).

    Replaced only by a complete pass.

    Attributes
    ----------
    sha : str
        A non-merge bulk commit. Primary key (with ``path``).
    path : str
        A selected core file it touched, as ``commit_file_change.path``.
        Primary key (with ``sha``).
    """

    sha: str = Field(primary_key=True, sa_type=Text)
    path: str = Field(primary_key=True, sa_type=Text)
