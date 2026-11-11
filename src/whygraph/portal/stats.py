"""A project's coverage counts, read from its WhyGraph DB (M2f-3 plan section 4.10).

One function, :func:`project_counts`, serves two readers: the project details
route (``ProjectDetails.stats``, the live counts the Overview shows) and the
scan runner, which stores the same counts in a finished run's
``summary.coverage`` so the Overview can chart coverage over time without
opening the project DB per list call.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlmodel import func, select

from whygraph.core.context import ProjectContext, use_project
from whygraph.core.safe_paths import UnsafePathError
from whygraph.db import get_session as project_session
from whygraph.db.models import Commit, CoreCommit, CoreState, RationaleCache
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.resources import _repo_overview_resource

from .db import data_dir
from .paths import check_project_db, check_project_paths

if TYPE_CHECKING:
    from .deps import PortalState


def project_counts(
    state: PortalState, ctx: ProjectContext, *, migrate: bool = True
) -> dict | None:
    """The coverage counts of an initialized project (blocking).

    Parameters
    ----------
    state : PortalState
        The portal state; its ``migrations`` bring the project DB to head
        before it is read.
    ctx : ProjectContext
        The project. Bound with :func:`~whygraph.core.context.use_project`
        for the reads, so a caller in a worker thread need not bind it.
    migrate : bool
        Whether to bring the DB to head first (the request path). The scan
        runner passes ``False``: it reads right after a scan
        child that migrated the DB itself, and Alembic's module-global
        state makes a migration off the request path a needless risk.

    Returns
    -------
    dict or None
        ``{commits, described, described_pct, pull_requests, issues,
        rationale_cards, core_commits, core_described,
        core_described_pct}``. ``rationale_cards`` counts **distinct**
        ``(path, line_start, line_end)`` ranges, so one range explained by
        two providers or models counts once (BUG-26). The three ``core_``
        counts cover the core set (commits only, no bulk slices) and are
        ``None`` before the first complete core pass, and the pct is also
        ``None`` when the core set is empty. ``None`` for a
        linked project (no local DB, the file untouched), when a DB path is
        a symlink (nothing is opened), when the DB does not exist yet, or
        when it cannot be read.
    """
    if ctx.remote is not None or ctx.config.whygraph_db is None:
        return None
    db_path = Path(ctx.config.whygraph_db)
    try:
        check_project_paths(ctx.root)
        check_project_db(data_dir(), db_path)
    except UnsafePathError:
        return None
    if not db_path.is_file():
        return None
    if migrate:
        try:
            state.migrations.ensure(ctx)
        except UnsafePathError:
            return None
    ranges = (
        select(RationaleCache.path, RationaleCache.line_start, RationaleCache.line_end)
        .distinct()
        .subquery()
    )
    try:
        with use_project(ctx):
            overview = _repo_overview_resource()
            with project_session() as session:
                cards = session.exec(select(func.count()).select_from(ranges)).one()
                core_state = session.get(CoreState, 1)
                core_ready = (
                    core_state is not None and core_state.completed_at is not None
                )
                core_commits = core_described = core_described_pct = None
                if core_ready:
                    core_commits = session.exec(
                        select(func.count()).select_from(CoreCommit)
                    ).one()
                    core_described = session.exec(
                        select(func.count())
                        .select_from(CoreCommit)
                        .join(Commit, CoreCommit.sha == Commit.sha)
                        .where(Commit.llm_description.is_not(None))
                    ).one()
                    if core_commits:
                        core_described_pct = round(
                            100 * core_described / core_commits, 1
                        )
    except WhyGraphError:
        return None
    coverage = overview["llm_description_coverage"]
    return {
        "commits": overview["counts"]["commits"],
        "described": coverage["described"],
        "described_pct": round(coverage["fraction"] * 100, 1),
        "pull_requests": overview["counts"]["pull_requests"],
        "issues": overview["counts"]["issues"],
        "rationale_cards": cards,
        "core_commits": core_commits,
        "core_described": core_described,
        "core_described_pct": core_described_pct,
    }


__all__ = ["project_counts"]
