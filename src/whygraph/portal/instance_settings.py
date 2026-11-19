"""Platform configuration: the ``instance_settings`` row and its cache (plan 59 §4.13, D46).

One singleton row holds the sign-up policy, who may create organizations, two
quotas and the announcement banner. The portal is exactly one process, so the
value lives in memory on ``state.instance_settings`` and is read from there;
the database is the source of truth across restarts.

The row is **not seeded** (``test_baseline_inserts_no_rows``): :func:`load`
answers the defaults while it is missing and :func:`save` upserts it. A failed
*read* must never fall back to the defaults, because the default sign-up policy
is ``open`` and would silently reopen a closed platform; the start-up caller
degrades the portal instead.

Every rule built on these values is enforced in production only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session

from .models import InstanceSetting

SIGNUP_POLICIES: tuple[str, ...] = ("open", "invite_only", "closed")
"""The values ``signup_policy`` accepts."""

ORG_CREATION: tuple[str, ...] = ("everyone", "admins")
"""The values ``org_creation`` accepts."""

BANNER_LEVELS: tuple[str, ...] = ("info", "warning")
"""The values ``banner_level`` accepts."""

BANNER_MAX = 500
"""The longest announcement, in characters."""

SETTING_KEYS: tuple[str, ...] = (
    "signup_policy",
    "org_creation",
    "max_orgs_per_user",
    "max_projects_per_org",
    "banner_text",
    "banner_level",
)
"""The columns a ``PUT`` may change."""

LOCK = threading.Lock()
"""Guards the cache swap: held from the read of the old values through the commit and the swap."""


@dataclass(frozen=True)
class InstanceSettings:
    """The platform configuration; the field defaults are the defaults.

    Attributes
    ----------
    signup_policy : str
        ``open``, ``invite_only`` or ``closed``.
    org_creation : str
        ``everyone`` or ``admins``.
    max_orgs_per_user : int or None
        Live orgs a user may own; ``None`` is no limit.
    max_projects_per_org : int or None
        Live projects per org; ``None`` is no limit.
    banner_text : str or None
        Plain-text announcement.
    banner_level : str
        ``info`` or ``warning``.
    updated_at : str or None
        ISO timestamp of the last change.
    updated_by : int or None
        ``users.id`` of the last writer.
    """

    signup_policy: str = "open"
    org_creation: str = "everyone"
    max_orgs_per_user: int | None = None
    max_projects_per_org: int | None = None
    banner_text: str | None = None
    banner_level: str = "info"
    updated_at: str | None = None
    updated_by: int | None = None

    def changeable(self) -> dict[str, Any]:
        """The values a ``PUT`` may change, by key."""
        return {key: getattr(self, key) for key in SETTING_KEYS}


def _from_row(row: InstanceSetting) -> InstanceSettings:
    return InstanceSettings(
        **{f.name: getattr(row, f.name) for f in fields(InstanceSettings)}
    )


def load(db: Session) -> InstanceSettings:
    """Read the row; the defaults when it does not exist yet.

    Parameters
    ----------
    db : Session
        A portal session.

    Returns
    -------
    InstanceSettings
        The stored values, or the defaults for a missing row. A database
        error propagates: it is never answered with the defaults.
    """
    row = db.get(InstanceSetting, 1)
    return InstanceSettings() if row is None else _from_row(row)


def save(
    db: Session,
    changes: dict[str, Any],
    by: int | None,
    *,
    publish: Callable[[InstanceSettings], None] | None = None,
) -> tuple[InstanceSettings, InstanceSettings]:
    """Apply ``changes`` (already validated) and upsert the row.

    The module lock is held from the read of the old values through the
    commit and ``publish``, so two concurrent writes cannot commit in one
    order and swap the cache in the other.

    Parameters
    ----------
    db : Session
        A portal session; this function commits it.
    changes : dict
        Only keys in :data:`SETTING_KEYS`; a key present with ``None``
        clears a nullable column.
    by : int or None
        ``users.id`` of the writer.
    publish : callable, optional
        Called with the new values after the commit, under the lock: the
        cache swap.

    Returns
    -------
    tuple of InstanceSettings
        ``(old, new)``.
    """
    with LOCK:
        old = load(db)
        new = replace(
            old,
            **changes,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            updated_by=by,
        )
        values = {f.name: getattr(new, f.name) for f in fields(InstanceSettings)}
        insert = pg_insert(InstanceSetting).values(id=1, **values)
        db.exec(  # type: ignore[call-overload]
            insert.on_conflict_do_update(
                index_elements=["id"],
                set_={key: insert.excluded[key] for key in values},
            )
        )
        db.commit()
        if publish is not None:
            publish(new)
        return old, new
