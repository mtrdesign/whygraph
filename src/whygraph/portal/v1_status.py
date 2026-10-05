"""A platform project as a connection token sees it: :class:`~whygraph.api_v1.StatusOut`.

Shared by the code exchange (``POST /api/connect/token``, which answers
with the linked project) and the ``/api/v1`` routes, whose every answer
carries ``project`` (M2e plan sections 4.4 and 4.5).
"""

from __future__ import annotations

from sqlmodel import Session, select

from whygraph.api_v1 import StatusOut
from whygraph.services.git import InvalidRepoUrlError
from whygraph.services.git.credentials import github_git_host

from .models import Membership, Project
from .routes import _github_full_name


def clone_url(project: Project) -> str | None:
    """The URL the platform clones ``project`` from, or ``None``.

    ``<git host>/<owner>/<name>.git`` on the platform's GitHub git host
    (:func:`~whygraph.services.git.credentials.github_git_host`) - the same
    URL the import cloned and every sync fetches - so a connected portal
    can match a checkout's ``origin`` against its host. ``None`` for a
    project that is not a GitHub import, or when the host is misconfigured.
    """
    full_name = _github_full_name(project)
    if full_name is None:
        return None
    try:
        return f"{github_git_host().url}/{full_name}.git"
    except InvalidRepoUrlError:
        return None


def status_out(project: Project, role: str) -> StatusOut:
    """Build the :class:`~whygraph.api_v1.StatusOut` of a ``projects`` row.

    Parameters
    ----------
    project : Project
        The loaded row.
    role : str
        The token user's role in the project's org (``owner`` / ``admin``
        / ``member``; never ``reader``).

    Returns
    -------
    StatusOut
        Slug, name, the GitHub identity, the scan state, ``access_lost`` and
        the role.
    """
    return StatusOut(
        slug=project.slug,
        name=project.name,
        github_full_name=_github_full_name(project),
        clone_url=clone_url(project),
        default_branch=project.default_branch,
        last_scanned_head=project.last_scanned_head,
        last_scan_at=project.last_scan_at,
        access_lost=project.access_lost_at is not None,
        role=role,  # type: ignore[arg-type] -- checked by StatusOut
    )


def load_status(db: Session, project_id: int, user_id: int) -> StatusOut | None:
    """The :class:`StatusOut` of ``project_id`` for ``user_id``, read now.

    Parameters
    ----------
    db : Session
        An open portal DB session.
    project_id : int
        ``projects.id``.
    user_id : int
        ``users.id``; the role is this user's ``memberships`` row in the
        project's org.

    Returns
    -------
    StatusOut or None
        ``None`` when the project is gone or the user holds no membership
        in its org.
    """
    row = db.exec(
        select(Project, Membership.role)
        .join(
            Membership,
            (Membership.org_id == Project.org_id) & (Membership.user_id == user_id),
        )
        .where(Project.id == project_id)
    ).first()
    if row is None:
        return None
    project, role = row
    return status_out(project, role)


__all__ = ["clone_url", "load_status", "status_out"]
