"""Path-history queries against the ``commit_file_change`` index.

Three pieces live here:

* :func:`resolve_path_aliases` — given a current path, walk
  ``renamed_from`` edges backwards (recursive CTE) and return every
  historical name that path has ever gone by. Used to make blame-less
  area-history queries rename-aware.
* :func:`path_commit_counts` — one grouped query giving the per-file
  commit count under a directory prefix, so an area outline can show
  where the churn is without a query per file.
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

from sqlalchemy import func
from sqlmodel import Session, col, select

from whygraph.analyze import CommitEvidence
from whygraph.core.utils import LIKE_ESCAPE_CHAR, like_escape
from whygraph.db import get_session
from whygraph.db.models import Commit, CommitFileChange
from whygraph.services.git import GitError, Repository

from .targets import repo_root


def branch_scope(current_branch: str | None):
    """SQL predicate for the commits an alias walk may see.

    The default branch, **plus** the branch you are standing on. Neither
    extreme is right: filtering to the default branch alone blinds
    WhyGraph to the rename you are making *right now* — close to the most
    valuable moment for a rationale lookup — while no filter at all lets
    an abandoned branch's renames pollute every path query forever.

    Self-cleaning, so no expiry is needed: merging promotes the rows to
    ``on_default_branch = 1`` and they stay visible on their own merit,
    and switching away drops the old branch's aliases immediately.

    Parameters
    ----------
    current_branch : str or None
        The checked-out branch, or ``None`` to scope to the default
        branch only — the safe direction, used for a detached HEAD and
        whenever git cannot be reached.

    Returns
    -------
    ColumnElement[bool]
        A predicate over the ``commit`` table; the caller must have
        joined it.
    """
    on_default = col(Commit.on_default_branch) == 1
    if current_branch is None:
        return on_default
    return on_default | (col(Commit.first_seen_ref) == current_branch)


def current_branch_scope() -> str | None:
    """The checked-out branch, or ``None`` when it should not widen the scope.

    Returns ``None`` on a detached HEAD — :attr:`Repository.current_branch`
    yields the literal ``"HEAD"`` there, which as a ``first_seen_ref``
    value would union in every commit ever scanned from a detached head —
    and on any :class:`GitError`. Both degrade to default-branch-only.

    Deliberately **uncached**: the MCP server is long-lived and outlives
    branch switches, so a cached value would serve exactly the stale
    aliases this scoping exists to prevent. One
    ``git rev-parse --abbrev-ref HEAD`` per tool call (~5 ms).

    Returns
    -------
    str or None
        The branch name, or ``None``.
    """
    try:
        branch = Repository(repo_root()).current_branch
    except GitError:
        return None
    return None if branch == "HEAD" else branch


def resolve_path_aliases(
    session: Session, path: str, *, current_branch: str | None = None
) -> set[str]:
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
    current_branch : str or None, optional
        Widen the walk to renames first seen on this branch, on top of
        the default branch — see :func:`branch_scope`. Defaults to
        ``None`` (default branch only), so a caller that does not thread
        it gets the conservative behaviour rather than an error.

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
            .join(Commit, col(Commit.sha) == col(CommitFileChange.commit_sha))
            .where(col(CommitFileChange.path).in_(frontier))
            .where(col(CommitFileChange.renamed_from).is_not(None))
            .where(branch_scope(current_branch))
        ).all()
        next_layer = {row for row in rows if row and row not in aliases}
        if not next_layer:
            break
        aliases.update(next_layer)
        frontier = next_layer
    return aliases


def path_commit_counts(path_prefix: str) -> dict[str, int]:
    """Default-branch commit count per file under ``path_prefix``.

    One grouped query for a whole subsystem, rather than a lookup per file:
    the caller is building an area outline and wants to know where the churn
    is, which is a per-file number over the same ``commit_file_change`` index
    :func:`area_history_commits` reads.

    Parameters
    ----------
    path_prefix : str
        A repo-relative directory or a single file path. A leading ``"./"``
        and a trailing ``"/"`` are tolerated, matching
        :meth:`whygraph.services.codegraph.CodeGraph.area`.

    Returns
    -------
    dict[str, int]
        ``path -> distinct commit count``. Paths with no scanned commit are
        absent rather than present-and-zero, so the caller decides how to
        render "never touched".

    Notes
    -----
    ``commit_file_change.path`` is the path *at that commit*, so a file moved
    mid-history is counted under each name it has had. Following the rename
    chain would need :func:`resolve_path_aliases` per file, which is a query
    per file — the wrong trade for a churn hint. :func:`area_history_commits`
    remains the rename-aware read.
    """
    prefix = path_prefix.strip().removeprefix("./").rstrip("/")
    if not prefix:
        return {}
    with get_session() as session:
        rows = session.exec(
            select(
                CommitFileChange.path,
                func.count(func.distinct(col(CommitFileChange.commit_sha))),
            )
            .join(Commit, col(Commit.sha) == col(CommitFileChange.commit_sha))
            .where(col(Commit.on_default_branch) == 1)
            .where(
                (col(CommitFileChange.path) == prefix)
                | col(CommitFileChange.path).like(
                    f"{like_escape(prefix)}/%", escape=LIKE_ESCAPE_CHAR
                )
            )
            .group_by(col(CommitFileChange.path))
        ).all()
    return {path: count for path, count in rows}


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
            aliases = resolve_path_aliases(
                session, path, current_branch=current_branch_scope()
            )
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
            # Area-history is a default-branch-only view, and this filter
            # is what enforces it. Flag-0 rows are no longer only PR-origin
            # recoveries (which carry no commit_file_change rows): they now
            # also include unmerged local work scanned off a feature
            # branch, which *does* carry them. The alias set above may
            # widen to the current branch; the commit set never does.
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
