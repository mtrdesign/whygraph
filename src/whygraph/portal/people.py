"""How org-facing pages name a person (plan 59 section 4.3.1).

A soft-deleted user stays in history - audit events, usage rows, scan
runs, notes, invitations - but every org-facing response that names them
shows :data:`DELETED_USER` instead of their name and login, from the
moment of the soft delete. The admin panel shows the real identity until
Erase, which rewrites the stored copies to the same label.
"""

from __future__ import annotations

DELETED_USER = "[Deleted User]"
"""The name a soft-deleted (or erased) user carries on org pages."""


def user_label(
    display_name: str, login: str | None, deleted_at: str | None
) -> tuple[str, str | None]:
    """The name and GitHub login an org page shows for a user.

    Parameters
    ----------
    display_name : str
        The user's display name.
    login : str or None
        The user's GitHub login.
    deleted_at : str or None
        ``users.deleted_at``: set for a soft-deleted user.

    Returns
    -------
    tuple of (str, str or None)
        ``(display_name, login)``, or ``(DELETED_USER, None)`` for a
        soft-deleted user.

    Examples
    --------
    >>> user_label("Ada", "ada", None)
    ('Ada', 'ada')
    >>> user_label("Ada", "ada", "2026-11-19T10:00:00+00:00")
    ('[Deleted User]', None)
    """
    if deleted_at is not None:
        return DELETED_USER, None
    return display_name, login


__all__ = ["DELETED_USER", "user_label"]
