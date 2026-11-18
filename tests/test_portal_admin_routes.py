"""The admin panel's routes (plan 59 section 4.6 onward).

Step 10 adds the Users & Admins API: the list's new columns, the user
detail, the session and connection-token revokes, the ``self_session``
guard, ``user_deleted`` / ``erased`` on every write, and the ``subject``
audit filter. Later steps add ``test_orgs_*``, ``test_audit_*`` and
``test_projects_*`` here.

The world is :func:`~test_portal_connect.cn`: Ada (a password instance
admin, admin-only), Ben (owner of ``acme``), Cy, Fay, Dee, Eve and Ivy (a
GitHub instance admin in no org). The gone-state cases set ``deleted_at``
/ ``erased_at`` on the row; step 12 adds the soft delete, restore and
erase routes' own state checks and the admin side of ``[Deleted User]``
(the real identity until Erase).
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import col, select

from test_portal_app import (  # noqa: F401 -- fixtures
    admin_signed_in,
    at,
    env,
    production_env,
    signed_in,
)
from test_portal_connect import (  # noqa: F401 -- `cn` is a fixture
    cn,
    issue,
    token_rows,
    uid_of_token,
)
from test_portal_identity_routes import audit_log, events  # noqa: F401 -- fixture
from whygraph.portal import audit_store, sessions
from whygraph.portal import db as portal_db
from whygraph.portal.models import (
    AuditEvent,
    ConnectionToken,
    Membership,
    User,
    UserSession,
)

ADMIN_LOGIN_REQUIRED = "admin_login_required"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def be_ada(n: SimpleNamespace) -> str:
    """Make Ada's admin session the client's one session; return its token."""
    n.client.cookies.clear()
    return admin_signed_in(n.client, n.ids["ada"])


def be_ivy_admin(n: SimpleNamespace, *, paired: bool) -> tuple[str, int, int | None]:
    """Make Ivy (a GitHub instance admin) the caller, with an admin session.

    Returns the admin token, its row id and (when ``paired``) the id of the
    everyday session it sits beside; the client holds the admin cookie only.
    """
    n.client.cookies.clear()
    with portal_db.get_session() as db:
        everyday = None
        if paired:
            token = sessions.create_session(db, n.ids["ivy"], "pytest")
            everyday = _row_id(db, token)
        admin = sessions.create_session(
            db, n.ids["ivy"], "pytest", kind="admin", paired_session_id=everyday
        )
        admin_id = _row_id(db, admin)
    base = n.client.app.state.portal.base_url
    n.client.cookies.set(sessions.admin_cookie_name(base), admin, domain=base.host)
    return admin, admin_id, everyday


def _row_id(db, token: str) -> int:  # noqa: ANN001
    return db.exec(
        select(UserSession.id).where(
            UserSession.token_hash == sessions.hash_token(token)
        )
    ).one()


def make_session(
    user_id: int, *, kind: str = "user", paired: int | None = None, agent="pytest"
) -> int:
    """Insert a session of ``user_id`` directly; return its row id."""
    with portal_db.get_session() as db:
        token = sessions.create_session(
            db, user_id, agent, kind=kind, paired_session_id=paired
        )
        return _row_id(db, token)


def session_ids(user_id: int) -> set[int]:
    with portal_db.get_session() as db:
        return set(
            db.exec(select(UserSession.id).where(UserSession.user_id == user_id)).all()
        )


def mark(user_id: int, **fields) -> None:
    """Set columns on a user row (``deleted_at``, ``erased_at``, ...)."""
    with portal_db.get_session() as db:
        user = db.get(User, user_id)
        for name, value in fields.items():
            setattr(user, name, value)
        db.add(user)


def url(path: str) -> str:
    return at() + path


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def audit_event(**fields) -> int:
    with portal_db.get_session() as db:
        row = AuditEvent(created_at=_iso(datetime.now(timezone.utc)), **fields)
        db.add(row)
        db.flush()
        return row.id


# ---------------------------------------------------------------------------
# GET /api/admin/users
# ---------------------------------------------------------------------------


def test_users_list_has_the_new_columns(cn: SimpleNamespace) -> None:
    be_ada(cn)
    response = cn.client.get(url("/api/admin/users"))
    assert response.status_code == 200, response.text
    rows = {r["uid"]: r for r in response.json()}
    ben, ada, cy = rows[cn.uids["ben"]], rows[cn.uids["ada"]], rows[cn.uids["cy"]]
    assert (ben["org_count"], ben["owner_of"]) == (1, 1)
    assert (cy["org_count"], cy["owner_of"]) == (1, 0)
    assert (ada["org_count"], ada["owner_of"]) == (0, 0)
    assert ada["has_password"] is True and ben["has_password"] is False
    assert "is_password" not in ada
    for row in rows.values():
        assert row["deleted_at"] is None and row["deleted_by"] is None
        assert row["erased"] is False
    # Ada's own session is an admin one: it does not count as "last session".
    assert ada["session_last_seen_at"] is None
    assert ben["session_last_seen_at"] is None
    signed_in(cn.client, cn.ids["ben"])
    be_ada(cn)
    seen = {r["uid"]: r for r in cn.client.get(url("/api/admin/users")).json()}
    assert seen[cn.uids["ben"]]["session_last_seen_at"] is not None
    assert seen[cn.uids["cy"]]["session_last_seen_at"] is None


def test_users_list_shows_the_deleted_state(cn: SimpleNamespace) -> None:
    mark(
        cn.ids["cy"],
        deleted_at="2026-11-01T10:00:00+00:00",
        deleted_by=cn.ids["ada"],
    )
    mark(cn.ids["eve"], deleted_at="2026-11-01T10:00:00+00:00", erased_at="2026-11-02")
    be_ada(cn)
    rows = {r["uid"]: r for r in cn.client.get(url("/api/admin/users")).json()}
    cy, eve = rows[cn.uids["cy"]], rows[cn.uids["eve"]]
    assert cy["deleted_at"] == "2026-11-01T10:00:00+00:00"
    assert cy["deleted_by"] == cn.uids["ada"]
    assert cy["erased"] is False
    assert eve["erased"] is True


# ---------------------------------------------------------------------------
# GET /api/admin/users/{uid}
# ---------------------------------------------------------------------------


def test_users_detail_collects_everything(cn: SimpleNamespace) -> None:
    ben = cn.ids["ben"]
    old = make_session(ben, agent="old browser")
    stale = make_session(ben, agent="stale browser")
    with portal_db.get_session() as db:
        db.get(UserSession, stale).expires_at = _iso(
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )
    make_session(ben, kind="admin", agent="admin browser")
    issue(cn, "ben", "api")
    for n in range(25):
        audit_event(event="login_success", actor_id=ben, actor_label="@ben", ip=f"{n}")
    audit_event(event="user_disabled", actor_id=cn.ids["ada"], target=cn.uids["ben"])
    audit_event(event="user_enabled", actor_id=cn.ids["ada"], target=cn.uids["cy"])
    be_ada(cn)

    response = cn.client.get(url(f"/api/admin/users/{cn.uids['ben']}"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["uid"] == cn.uids["ben"] and body["github_login"] == "ben"
    assert (body["org_count"], body["owner_of"]) == (1, 1)
    assert body["deleted_at"] is None and body["erased"] is False
    assert body["memberships"] == [
        {"org": "acme", "name": "Acme", "role": "owner", "owner": True}
    ]
    assert body["spend"] == [{"org": "acme", "spent_usd": 0.0}]

    ids = {s["id"]: s for s in body["sessions"]}
    assert old in ids and stale not in ids  # past its lifetime: left out
    assert {s["kind"] for s in body["sessions"]} == {"user", "admin"}
    for row in body["sessions"]:
        assert set(row) == {"id", "kind", "created_at", "last_seen_at", "user_agent"}

    (token,) = body["tokens"]
    assert token["org"] == "acme" and token["project"] == "api"
    assert token["revoked_at"] is None
    assert "token_hash" not in token and "token_hash" not in response.text

    # The latest 20, newest first: the actor's and the target's events.
    assert len(body["events"]) == 20
    assert body["events"][0]["event"] == "user_disabled"


def test_users_detail_of_a_soft_deleted_user_resolves_the_snapshot(
    cn: SimpleNamespace,
) -> None:
    import json

    with portal_db.get_session() as db:
        member = db.exec(
            select(Membership).where(Membership.user_id == cn.ids["cy"])
        ).one()
        snapshot = [
            {"org_id": member.org_id, "role": member.role},
            {"org_id": 99999, "role": "member"},  # a purged org: skipped
        ]
        db.delete(member)
    mark(
        cn.ids["cy"],
        deleted_at="2026-11-01T10:00:00+00:00",
        deleted_by=cn.ids["ada"],
        deleted_memberships=json.dumps(snapshot),
    )
    be_ada(cn)
    body = cn.client.get(url(f"/api/admin/users/{cn.uids['cy']}")).json()
    assert body["deleted_at"] == "2026-11-01T10:00:00+00:00"
    assert body["deleted_by"] == cn.uids["ada"]
    assert body["memberships"] == [
        {"org": "acme", "name": "Acme", "role": "member", "owner": False}
    ]


def test_users_detail_unknown_is_404_and_needs_an_admin_session(
    cn: SimpleNamespace,
) -> None:
    be_ada(cn)
    assert cn.client.get(url("/api/admin/users/nobody")).status_code == 404
    cn.client.cookies.clear()
    signed_in(cn.client, cn.ids["ben"])  # an everyday session opens nothing
    response = cn.client.get(url(f"/api/admin/users/{cn.uids['ben']}"))
    assert response.status_code == 401
    assert response.json()["code"] == ADMIN_LOGIN_REQUIRED


# ---------------------------------------------------------------------------
# The subject audit filter
# ---------------------------------------------------------------------------


def test_users_subject_filter_matches_the_actor_and_the_target(
    cn: SimpleNamespace,
) -> None:
    ben, ben_uid = cn.ids["ben"], cn.uids["ben"]
    as_actor = audit_event(event="login_success", actor_id=ben)
    as_target = audit_event(
        event="user_disabled", actor_id=cn.ids["ada"], target=ben_uid
    )
    audit_event(event="user_disabled", actor_id=cn.ids["ada"], target=cn.uids["cy"])
    audit_event(event="login_success", actor_id=cn.ids["cy"])
    found, _next = audit_store.query_events(audit_store.make_filter(subject=ben_uid))
    assert [e["id"] for e in found] == [as_target, as_actor]
    # `actor` alone still matches only the actor.
    only_actor, _ = audit_store.query_events(audit_store.make_filter(actor=ben_uid))
    assert [e["id"] for e in only_actor] == [as_actor]
    nobody, _ = audit_store.query_events(audit_store.make_filter(subject="nobody"))
    assert nobody == []


# ---------------------------------------------------------------------------
# PATCH, reset-link and the gone states
# ---------------------------------------------------------------------------


GONE = [
    ({"deleted_at": "2026-11-01T10:00:00+00:00"}, "user_deleted"),
    ({"deleted_at": "2026-11-01T10:00:00+00:00", "erased_at": "2026-11-02"}, "erased"),
]


def _writes(n: SimpleNamespace, uid: str):  # noqa: ANN202
    """Every write route on one user, as ``(method, path, body)``."""
    return [
        ("PATCH", f"/api/admin/users/{uid}", {"disabled": True}),
        ("POST", f"/api/admin/users/{uid}/reset-link", None),
        ("DELETE", f"/api/admin/users/{uid}/sessions", None),
        ("DELETE", f"/api/admin/users/{uid}/sessions/1", None),
        ("DELETE", f"/api/admin/users/{uid}/tokens/sometoken", None),
        ("DELETE", f"/api/admin/users/{uid}", {"confirm": "cy"}),
    ]


@pytest.mark.parametrize(("columns", "code"), GONE)
def test_users_writes_on_a_deleted_or_erased_user_are_409(
    cn: SimpleNamespace, columns: dict, code: str
) -> None:
    mark(cn.ids["cy"], **columns)
    be_ada(cn)
    for method, path, body in _writes(cn, cn.uids["cy"]):
        response = cn.client.request(method, url(path), json=body)
        assert response.status_code == 409, (method, path, response.text)
        assert response.json()["code"] == code, (method, path)
    with portal_db.get_session() as db:
        assert db.get(User, cn.ids["cy"]).disabled_at is None  # nothing changed


# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{uid}/sessions
# ---------------------------------------------------------------------------


def test_users_sessions_revoke_all_ends_both_kinds(
    cn: SimpleNamespace, audit_log
) -> None:
    ivy = cn.ids["ivy"]
    everyday = make_session(ivy)
    admin = make_session(ivy, kind="admin", paired=everyday)
    other = make_session(cn.ids["ben"])
    be_ada(cn)
    response = cn.client.delete(url(f"/api/admin/users/{cn.uids['ivy']}/sessions"))
    assert response.status_code == 200, response.text
    assert response.json() == {"revoked": 2}
    assert session_ids(ivy) == set()
    assert other in session_ids(cn.ids["ben"])  # another user's stays
    (event,) = [e for e in events(audit_log) if e["event"] == "sessions_revoked"]
    assert event["uid"] == cn.uids["ada"] and event["target"] == cn.uids["ivy"]
    assert event["kind"] == "all" and event["count"] == 2
    assert admin not in session_ids(ivy)


def test_users_sessions_revoke_one_kind(cn: SimpleNamespace) -> None:
    ivy = cn.ids["ivy"]
    everyday = make_session(ivy)
    admin = make_session(ivy, kind="admin", paired=everyday)
    unpaired = make_session(ivy, kind="admin")
    be_ada(cn)
    # `user` ends the everyday row and the admin session paired with it
    # (D51), not an unpaired admin session.
    response = cn.client.request(
        "DELETE",
        url(f"/api/admin/users/{cn.uids['ivy']}/sessions"),
        json={"kind": "user"},
    )
    assert response.json() == {"revoked": 2}
    assert session_ids(ivy) == {unpaired}
    assert admin not in session_ids(ivy)
    response = cn.client.request(
        "DELETE",
        url(f"/api/admin/users/{cn.uids['ivy']}/sessions"),
        json={"kind": "admin"},
    )
    assert response.json() == {"revoked": 1}
    assert session_ids(ivy) == set()
    bad = cn.client.request(
        "DELETE",
        url(f"/api/admin/users/{cn.uids['ivy']}/sessions"),
        json={"kind": "nope"},
    )
    assert bad.status_code == 422


def test_users_sessions_revoke_refuses_to_end_the_callers_own_admin_session(
    cn: SimpleNamespace, audit_log
) -> None:
    be_ada(cn)
    own = url(f"/api/admin/users/{cn.uids['ada']}/sessions")
    for body in (None, {"kind": "admin"}):
        response = cn.client.request("DELETE", own, json=body)
        assert response.status_code == 409, (body, response.text)
        assert response.json()["code"] == "self_session"
    # `user` on your own account is fine when no everyday session is paired.
    assert cn.client.request("DELETE", own, json={"kind": "user"}).status_code == 200
    # Ivy, with her admin session paired to an everyday one.
    _, admin_id, everyday = be_ivy_admin(cn, paired=True)
    mine = url(f"/api/admin/users/{cn.uids['ivy']}/sessions")
    response = cn.client.request("DELETE", mine, json={"kind": "user"})
    assert response.status_code == 409
    assert response.json()["code"] == "self_session"
    assert {admin_id, everyday} <= session_ids(cn.ids["ivy"])
    # Ivy may end another user's everyday sessions (and their paired admin).
    other = make_session(cn.ids["cy"])
    ok = cn.client.request(
        "DELETE", url(f"/api/admin/users/{cn.uids['cy']}/sessions"), json=None
    )
    assert ok.json() == {"revoked": 1}
    assert other not in session_ids(cn.ids["cy"])
    assert [e for e in events(audit_log) if e["event"] == "sessions_revoked"]


# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{uid}/sessions/{id}
# ---------------------------------------------------------------------------


def test_users_session_delete_filters_on_the_user(
    cn: SimpleNamespace, audit_log
) -> None:
    benrow = make_session(cn.ids["ben"])
    cyrow = make_session(cn.ids["cy"])
    be_ada(cn)
    # Cy's session id under Ben's uid: nothing matches, nothing is deleted.
    miss = cn.client.delete(url(f"/api/admin/users/{cn.uids['ben']}/sessions/{cyrow}"))
    assert miss.status_code == 404
    assert cyrow in session_ids(cn.ids["cy"])
    ok = cn.client.delete(url(f"/api/admin/users/{cn.uids['ben']}/sessions/{benrow}"))
    assert ok.status_code == 200 and ok.json() == {"revoked": 1}
    assert benrow not in session_ids(cn.ids["ben"])
    (event,) = [e for e in events(audit_log) if e["event"] == "sessions_revoked"]
    assert event["target"] == cn.uids["ben"] and event["count"] == 1
    assert event["kind"] == "user"


def test_users_session_delete_of_an_everyday_row_ends_the_paired_admin(
    cn: SimpleNamespace,
) -> None:
    ivy = cn.ids["ivy"]
    everyday = make_session(ivy)
    admin = make_session(ivy, kind="admin", paired=everyday)
    be_ada(cn)
    response = cn.client.delete(
        url(f"/api/admin/users/{cn.uids['ivy']}/sessions/{everyday}")
    )
    assert response.status_code == 200
    assert session_ids(ivy) == set()
    assert admin not in session_ids(ivy)


def test_users_session_delete_refuses_the_callers_own_sessions(
    cn: SimpleNamespace,
) -> None:
    _, admin_id, everyday = be_ivy_admin(cn, paired=True)
    unpaired = make_session(cn.ids["ivy"])
    base = f"/api/admin/users/{cn.uids['ivy']}/sessions"
    for target in (admin_id, everyday):
        response = cn.client.delete(url(f"{base}/{target}"))
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "self_session"
    assert {admin_id, everyday} <= session_ids(cn.ids["ivy"])
    # Her other everyday session is not the one she is paired with.
    assert cn.client.delete(url(f"{base}/{unpaired}")).status_code == 200


# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{uid}/tokens/{token_uid}
# ---------------------------------------------------------------------------


def test_users_token_revoke_audits_with_the_org_and_project(
    cn: SimpleNamespace, audit_log
) -> None:
    token = issue(cn, "ben", "api")
    token_uid = uid_of_token(token)
    be_ada(cn)
    response = cn.client.delete(
        url(f"/api/admin/users/{cn.uids['ben']}/tokens/{token_uid}")
    )
    assert response.status_code == 204, response.text
    (row,) = [r for r in token_rows() if r.uid == token_uid]
    assert row.revoked_at is not None and row.revoked_reason == "admin_revoked"
    (event,) = [e for e in events(audit_log) if e["event"] == "connection_revoked"]
    assert event["uid"] == cn.uids["ada"] and event["target"] == cn.uids["ben"]
    assert event["org_id"] == cn.orgs["acme"] and event["org"] == "acme"
    assert event["project"] == "api" and event["token_uid"] == token_uid
    assert event["via"] == "instance_admin" and event["reason"] == "admin_revoked"
    assert token not in str(event)
    # A second revoke finds no live token.
    again = cn.client.delete(
        url(f"/api/admin/users/{cn.uids['ben']}/tokens/{token_uid}")
    )
    assert again.status_code == 404


def test_users_token_revoke_is_scoped_to_the_user(cn: SimpleNamespace) -> None:
    token_uid = uid_of_token(issue(cn, "ben", "api"))
    be_ada(cn)
    response = cn.client.delete(
        url(f"/api/admin/users/{cn.uids['cy']}/tokens/{token_uid}")
    )
    assert response.status_code == 404
    with portal_db.get_session() as db:
        live = db.exec(
            select(ConnectionToken).where(
                col(ConnectionToken.uid) == token_uid,
                col(ConnectionToken.revoked_at).is_(None),
            )
        ).all()
    assert len(live) == 1


def test_users_routes_need_an_admin_session(cn: SimpleNamespace) -> None:
    signed_in(cn.client, cn.ids["ben"])
    for method, path, body in _writes(cn, cn.uids["cy"]):
        response = cn.client.request(method, url(path), json=body)
        assert response.status_code == 401, (method, path)
        assert response.json()["code"] == ADMIN_LOGIN_REQUIRED


# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{uid}, .../restore, .../erase (plan 59 section 4.3.1)
# ---------------------------------------------------------------------------


def test_users_restore_and_erase_answer_by_the_deleted_state(
    cn: SimpleNamespace,
) -> None:
    be_ada(cn)
    cy = cn.uids["cy"]
    for path, body in (
        ("/api/admin/users/nobody", {"confirm": "x"}),
        ("/api/admin/users/nobody/restore", None),
        ("/api/admin/users/nobody/erase", {"confirm": "x"}),
    ):
        method = "DELETE" if path.endswith("nobody") else "POST"
        assert cn.client.request(method, url(path), json=body).status_code == 404
    # A live account: neither restored nor erased.
    for path, body in (
        (f"/api/admin/users/{cy}/restore", None),
        (f"/api/admin/users/{cy}/erase", {"confirm": "cy"}),
    ):
        response = cn.client.post(url(path), json=body)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "not_deleted"
    # An erased one: nothing at all.
    mark(cn.ids["cy"], deleted_at="2026-11-01T10:00:00+00:00", erased_at="2026-11-02")
    for path, body in (
        (f"/api/admin/users/{cy}/restore", None),
        (f"/api/admin/users/{cy}/erase", {"confirm": "cy"}),
    ):
        response = cn.client.post(url(path), json=body)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "erased"


def test_users_admin_reads_show_a_deleted_users_real_identity(
    cn: SimpleNamespace,
) -> None:
    audit_event(
        event="login_success",
        actor_id=cn.ids["cy"],
        actor_label="@cy",
        fields={"github_login": "cy", "email": "cy@example.com"},
    )
    be_ada(cn)
    deleted = cn.client.request(
        "DELETE", url(f"/api/admin/users/{cn.uids['cy']}"), json={"confirm": "cy"}
    )
    assert deleted.status_code == 200, deleted.text
    rows = {r["uid"]: r for r in cn.client.get(url("/api/admin/users")).json()}
    row = rows[cn.uids["cy"]]
    assert (row["display_name"], row["github_login"]) == ("Cy", "cy")
    assert row["deleted_at"] is not None and row["deleted_by"] == cn.uids["ada"]
    detail = cn.client.get(url(f"/api/admin/users/{cn.uids['cy']}")).json()
    assert detail["memberships"] == [
        {"org": "acme", "name": "Acme", "role": "member", "owner": False}
    ]
    (login,) = [e for e in detail["events"] if e["event"] == "login_success"]
    assert login["actor"]["label"] == "@cy"
    assert login["fields"]["email"] == "cy@example.com"
    audit = cn.client.get(url("/api/admin/audit"), params={"actor": "@cy"}).json()
    assert [e["actor"]["label"] for e in audit["events"]] == ["@cy"]
