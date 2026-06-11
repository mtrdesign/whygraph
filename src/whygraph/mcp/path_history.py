"""Path-history queries against the ``commit_file_change`` index.

Two pieces live here:

* :func:`resolve_path_aliases` — given a current path, walk
  ``renamed_from`` edges backwards (recursive CTE) and return every
  historical name that path has ever gone by. Used to make blame-less
  area-history queries rename-aware.
* :func:`area_history_commits` — given a path (and optionally a
  pre-resolved alias set), return the :class:`CommitEvidence` bundles
  for every scanned commit that touched any alias, newest first.

Lives in :mod:`whygraph.mcp` (not :mod:`whygraph.db`) because the
output shape is what MCP tools return — :func:`area_history_commits`
hands back the same :class:`~whygraph.analyze.CommitEvidence` value the
existing evidence collector produces, so the rationale generator and
the JSON serialisers stay unchanged.
"""

from __future__ import annotations

from sqlmodel import Session, col, select

from whygraph.analyze import CommitEvidence
from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange


def resolve_path_aliases(session: Session, path: str) -> set[str]:
    """Every historical name ``path`` has ever gone by, plus ``path`` itself.

    Walks ``commit_file_change.renamed_from`` edges one BFS layer at a
    time. A typical rename chain is shallow (zero to a handful of edges)
    so a Python loop is simpler than a recursive CTE and stays inside
    the typed SQLModel surface.

    Parameters
    ----------
    session : Session
        An open SQLModel/SQLAlchemy session.
    path : str
        The path to start from — typically the current HEAD path of the
        file the caller cares about. Returned in the result set.

    Returns
    -------
    set[str]
        Every alias, including the seed. Empty input ⇒ empty output.
    """
    if not path:
        return set()
    aliases: set[str] = {path}
    frontier: set[str] = {path}
    while frontier:
        rows = session.exec(
            select(CommitFileChange.renamed_from)
            .where(col(CommitFileChange.path).in_(frontier))
            .where(col(CommitFileChange.renamed_from).is_not(None))
        ).all()
        next_layer = {row for row in rows if row and row not in aliases}
        if not next_layer:
            break
        aliases.update(next_layer)
        frontier = next_layer
    return aliases


def area_history_commits(
    path: str,
    *,
    limit: int = 20,
    include_renames: bool = True,
    exclude_shas: set[str] | None = None,
) -> list[CommitEvidence]:
    """Commits that touched ``path`` (or any historical alias), newest first.

    The returned bundle has the same shape :func:`whygraph.mcp.evidence.collect_evidence`
    produces, so callers can merge area-history into a blame-derived list
    without translation. PR/issue joins are computed lazily by the
    evidence module; this function only resolves the commit set.

    Parameters
    ----------
    path : str
        The path the caller cares about, as it appears at HEAD (or any
        commit, really — the alias walk handles both directions of the
        rename chain).
    limit : int, optional
        Cap on the number of commits returned. Default 20.
    include_renames : bool, optional
        When ``True`` (default), the alias chain is walked and commits
        for any historical name are included. When ``False``, only the
        literal ``path`` is matched — useful for tools that want a
        strictly-current-path view.
    exclude_shas : set[str] or None, optional
        SHAs to omit (typically the blame-derived set when this function
        is called as the "fill the rest" half of an evidence merge).

    Returns
    -------
    list[CommitEvidence]
        Newest first, capped at ``limit``. Empty when no scanned commit
        touched the path.
    """
    # Imported lazily to keep this module free of an evidence-import cycle.
    from .evidence import _linked_issues, _linked_prs

    with get_session() as session:
        if include_renames:
            aliases = resolve_path_aliases(session, path)
        else:
            aliases = {path}
        if not aliases:
            return []
        stmt = (
            select(Commit)
            .join(
                CommitFileChange,
                col(CommitFileChange.commit_sha) == col(Commit.sha),
            )
            .where(col(CommitFileChange.path).in_(aliases))
            # Area-history is a main-walk-only view. Recovered PR-origin
            # commits (on_default_branch=0) carry no commit_file_change
            # rows so the join already excludes them; this makes the
            # invariant explicit for a future broad consumer.
            .where(col(Commit.on_default_branch) == 1)
        )
        if exclude_shas:
            stmt = stmt.where(col(Commit.sha).not_in(exclude_shas))
        # ``distinct()`` collapses the join's duplicates (one row per
        # touched alias per commit) before ordering / capping.
        stmt = stmt.distinct().order_by(col(Commit.committed_at).desc()).limit(limit)
        commits = list(session.exec(stmt).all())

        items: list[CommitEvidence] = []
        for commit in commits:
            prs = _linked_prs(session, commit.sha)
            issues = _linked_issues(session, prs)
            items.append(
                CommitEvidence(commit, tuple(prs), tuple(issues), source="area")
            )
        session.expunge_all()
    return items
