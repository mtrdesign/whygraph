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
from decimal import Decimal
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
from whygraph.portal import orgs as orgs_mod
from whygraph.portal import db as portal_db
from whygraph.portal.budgets import reload_org_budgets
from whygraph.portal.models import (
    AgentCallDay,
    AuditEvent,
    Budget,
    ConnectionToken,
    Invitation,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    RetiredOrgSlug,
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


# ---------------------------------------------------------------------------
# Organizations (plan 59 section 4.7)
# ---------------------------------------------------------------------------


def org_row(slug: str):  # noqa: ANN201
    with portal_db.get_session() as db:
        row = db.exec(select(Organization).where(Organization.slug == slug)).one()
        db.expunge(row)
        return row


def set_org(slug: str, **fields) -> None:
    with portal_db.get_session() as db:
        row = db.exec(select(Organization).where(Organization.slug == slug)).one()
        for name, value in fields.items():
            setattr(row, name, value)
        db.add(row)


def members_of(slug: str) -> dict[str, str]:
    """``uid -> role`` of an org."""
    with portal_db.get_session() as db:
        return {
            uid: role
            for uid, role in db.exec(
                select(User.uid, Membership.role)
                .join(Membership, col(Membership.user_id) == col(User.id))
                .join(Organization, col(Organization.id) == col(Membership.org_id))
                .where(Organization.slug == slug)
            ).all()
        }


def test_orgs_list_has_the_new_fields_and_includes_deleted_ones(
    cn: SimpleNamespace,
) -> None:
    now = _iso(datetime.now(timezone.utc))
    with portal_db.get_session() as db:
        db.add(
            AgentCallDay(
                org_id=cn.orgs["acme"],
                project_id=cn.projects["api"],
                day="2026-11-05",
                source="mcp",
                kind="whygraph_evidence_for",
                calls=1,
            )
        )
        db.add(
            Project(
                org_id=cn.orgs["acme"],
                slug="gone",
                name="Gone",
                source="github",
                root="x",
                deleted_at=now,
            )
        )
    set_org("bravo", deleted_at=now, deleted_by=cn.ids["ada"])
    set_org("acme", suspended_at=now, suspended_reason="abuse")
    be_ada(cn)
    response = cn.client.get(url("/api/admin/orgs"))
    assert response.status_code == 200, response.text
    rows = {r["slug"]: r for r in response.json()}
    assert set(rows) == {"acme", "bravo"}  # the soft-deleted one is listed
    acme, bravo = rows["acme"], rows["bravo"]
    assert acme["project_count"] == 2  # the soft-deleted project is not counted
    assert acme["owners"] == ["ben"] and acme["member_count"] == 3
    assert acme["suspended"] == {"at": now, "reason": "abuse"}
    assert acme["deleted_at"] is None and acme["deleted_by"] is None
    assert acme["last_activity_day"] == "2026-11-05"
    assert acme["storage_bytes"] is None and acme["spend_mtd"] == 0
    assert bravo["deleted_at"] == now and bravo["deleted_by"] == cn.uids["ada"]
    assert bravo["suspended"] is None and bravo["last_activity_day"] is None
    assert bravo["owners"] == ["dee"]


def test_orgs_detail_collects_members_projects_budgets_and_counts(
    cn: SimpleNamespace,
) -> None:
    with portal_db.get_session() as db:
        db.add(
            Invitation(
                org_id=cn.orgs["acme"],
                github_id=999,
                github_login="newbie",
                role="member",
                invited_by=cn.ids["ben"],
                created_at=_iso(datetime.now(timezone.utc)),
                expires_at=_iso(datetime.now(timezone.utc) + timedelta(days=3)),
            )
        )
        db.add(
            Project(
                org_id=cn.orgs["acme"],
                slug="gone",
                name="Gone",
                source="github",
                root="x",
                deleted_at=_iso(datetime.now(timezone.utc)),
            )
        )
        db.add(
            Budget(
                org_id=cn.orgs["acme"],
                scope="org",
                monthly_usd=Decimal("12.5"),
                hard_stop=True,
            )
        )
    reload_org_budgets(cn.state.budgets, cn.orgs["acme"])
    audit_event(event="org_renamed", org_id=cn.orgs["acme"], org_slug="acme")
    be_ada(cn)
    response = cn.client.get(url("/api/admin/orgs/acme"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["slug"], body["name"]) == ("acme", "Acme")
    assert body["suspended"] is None and body["deleted_at"] is None
    now = _iso(datetime.now(timezone.utc))
    set_org(
        "acme", suspended_at=now, suspended_reason="abuse", suspended_by=cn.ids["ada"]
    )
    again = cn.client.get(url("/api/admin/orgs/acme")).json()
    assert again["suspended"] == {"at": now, "reason": "abuse", "by": "Ada"}
    set_org("acme", suspended_by=None)
    assert cn.client.get(url("/api/admin/orgs/acme")).json()["suspended"]["by"] is None
    set_org("acme", suspended_at=None, suspended_reason=None)
    members = {m["github_login"]: m for m in body["members"]}
    assert {k: v["role"] for k, v in members.items()} == {
        "ben": "owner",
        "cy": "member",
        "fay": "admin",
    }
    assert members["ben"]["uid"] == cn.uids["ben"]
    assert members["ben"]["has_password"] is False
    assert body["open_invitations"] == 1
    projects = {p["slug"]: p for p in body["projects"]}
    assert set(projects) == {"api", "web", "gone"}  # soft-deleted included
    assert projects["gone"]["deleted_at"] is not None
    assert projects["api"]["source"] == "github"
    assert projects["api"]["storage_bytes"] is None
    assert body["budgets"] == [
        {
            "scope": "org",
            "monthly_usd": 12.5,
            "hard_stop": True,
            "spent_usd": 0,
            "project": None,
            "member": None,
        }
    ]
    assert body["rows"]["audit_events"] >= 1
    assert body["rows"]["usage_events"] == 0
    assert body["rows"]["agent_call_days"] == 0
    assert body["spend_mtd"] == 0
    assert cn.client.get(url("/api/admin/orgs/nope")).status_code == 404
    # A soft-deleted org still has a detail page.
    set_org("bravo", deleted_at=_iso(datetime.now(timezone.utc)))
    deleted = cn.client.get(url("/api/admin/orgs/bravo")).json()
    assert deleted["deleted_at"] is not None


def test_orgs_create_makes_a_github_account_the_owner(
    cn: SimpleNamespace, audit_log
) -> None:
    be_ada(cn)
    response = cn.client.post(
        url("/api/admin/orgs"),
        json={"slug": "newco", "name": " New Co ", "owner_uid": cn.uids["eve"]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["slug"] == "newco" and response.json()["name"] == "New Co"
    assert members_of("newco") == {cn.uids["eve"]: "owner"}
    (event,) = [e for e in events(audit_log) if e["event"] == "org_created"]
    assert event["target"] == cn.uids["eve"] and event["via"] == "instance_admin"
    assert event["org"] == "newco"


def test_orgs_create_refuses_bad_slugs_and_taken_ones(cn: SimpleNamespace) -> None:
    be_ada(cn)

    def create(slug: str, owner: str = "eve"):  # noqa: ANN202
        return cn.client.post(
            url("/api/admin/orgs"),
            json={"slug": slug, "name": "X", "owner_uid": cn.uids[owner]},
        )

    assert create("Bad Slug").status_code == 422
    assert create("Bad Slug").json()["code"] == "bad_slug"
    assert create("acme").json()["code"] == "slug_taken"  # live
    set_org("bravo", deleted_at=_iso(datetime.now(timezone.utc)))
    assert create("bravo").json()["code"] == "slug_taken"  # soft-deleted
    with portal_db.get_session() as db:
        db.add(RetiredOrgSlug(slug="retired"))
    assert create("retired").json()["code"] == "slug_taken"  # retired
    assert create("fresh").status_code == 201
    unknown = cn.client.post(
        url("/api/admin/orgs"),
        json={"slug": "other", "name": "X", "owner_uid": "nobody"},
    )
    assert unknown.status_code == 404


def test_orgs_create_refuses_a_password_disabled_or_deleted_owner(
    cn: SimpleNamespace,
) -> None:
    be_ada(cn)

    def create(owner_uid: str):  # noqa: ANN202
        return cn.client.post(
            url("/api/admin/orgs"),
            json={"slug": "newco", "name": "N", "owner_uid": owner_uid},
        )

    password = create(cn.uids["ada"])
    assert (password.status_code, password.json()["code"]) == (
        409,
        "github_account_required",
    )
    mark(cn.ids["eve"], disabled_at=_iso(datetime.now(timezone.utc)))
    assert create(cn.uids["eve"]).json()["code"] == "user_disabled"
    mark(
        cn.ids["dee"],
        deleted_at=_iso(datetime.now(timezone.utc)),
    )
    deleted = create(cn.uids["dee"])
    assert (deleted.status_code, deleted.json()["code"]) == (409, "user_deleted")
    with portal_db.get_session() as db:
        assert (
            db.exec(select(Organization.id).where(Organization.slug == "newco")).first()
            is None
        )


def test_orgs_rename_audits_and_works_on_a_suspended_org(
    cn: SimpleNamespace, audit_log
) -> None:
    set_org("acme", suspended_at=_iso(datetime.now(timezone.utc)))  # D33
    be_ada(cn)
    response = cn.client.patch(
        url("/api/admin/orgs/acme"), json={"name": "  Acme Two "}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"slug": "acme", "name": "Acme Two"}
    assert org_row("acme").name == "Acme Two"
    (event,) = [e for e in events(audit_log) if e["event"] == "org_renamed"]
    assert event["via"] == "instance_admin" and event["previous"] == "Acme"
    blank = cn.client.patch(url("/api/admin/orgs/acme"), json={"name": "   "})
    assert (blank.status_code, blank.json()["code"]) == (422, "bad_name")
    assert (
        cn.client.patch(url("/api/admin/orgs/nope"), json={"name": "x"}).status_code
        == 404
    )


def test_orgs_add_a_member_by_uid(cn: SimpleNamespace, audit_log) -> None:
    be_ada(cn)
    response = cn.client.post(
        url("/api/admin/orgs/acme/members"),
        json={"uid": cn.uids["eve"], "role": "admin"},
    )
    assert response.status_code == 200, response.text
    row = response.json()
    assert (row["uid"], row["role"], row["github_login"]) == (
        cn.uids["eve"],
        "admin",
        "eve",
    )
    assert members_of("acme")[cn.uids["eve"]] == "admin"
    (event,) = [e for e in events(audit_log) if e["event"] == "member_added"]
    assert event["target"] == cn.uids["eve"] and event["via"] == "instance_admin"
    again = cn.client.post(
        url("/api/admin/orgs/acme/members"),
        json={"uid": cn.uids["eve"], "role": "member"},
    )
    assert again.json()["code"] == "already_member"
    bad = cn.client.post(
        url("/api/admin/orgs/acme/members"),
        json={"uid": cn.uids["dee"], "role": "reader"},
    )
    assert bad.json()["code"] == "bad_role"
    unknown = cn.client.post(
        url("/api/admin/orgs/acme/members"), json={"uid": "nobody", "role": "member"}
    )
    assert unknown.status_code == 404


def test_orgs_add_a_member_reads_the_user_first_and_refuses_gone_accounts(
    cn: SimpleNamespace,
) -> None:
    be_ada(cn)

    def add(uid: str, role: str = "member"):  # noqa: ANN202
        return cn.client.post(
            url("/api/admin/orgs/acme/members"), json={"uid": uid, "role": role}
        )

    mark(cn.ids["eve"], deleted_at=_iso(datetime.now(timezone.utc)))
    assert add(cn.uids["eve"]).json()["code"] == "user_deleted"
    mark(cn.ids["dee"], disabled_at=_iso(datetime.now(timezone.utc)))
    assert add(cn.uids["dee"]).json()["code"] == "user_disabled"
    owner = add(cn.uids["ada"], "owner")  # a password account
    assert (owner.status_code, owner.json()["code"]) == (409, "github_account_required")
    assert add(cn.uids["ada"], "member").status_code == 200  # but may be a member
    assert cn.uids["eve"] not in members_of("acme")
    assert cn.uids["dee"] not in members_of("acme")


def test_orgs_role_changes_keep_the_last_owner_rule_and_transfer_ownership(
    cn: SimpleNamespace, audit_log
) -> None:
    be_ada(cn)
    base = "/api/admin/orgs/acme/members/"
    last = cn.client.patch(url(base + cn.uids["ben"]), json={"role": "member"})
    assert (last.status_code, last.json()["code"]) == (409, "last_owner")
    remove_last = cn.client.delete(url(base + cn.uids["ben"]))
    assert remove_last.json()["code"] == "last_owner"
    # A hand-over: promote another member, then the first owner may go.
    promoted = cn.client.patch(url(base + cn.uids["cy"]), json={"role": "owner"})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "owner"
    demoted = cn.client.patch(url(base + cn.uids["ben"]), json={"role": "member"})
    assert demoted.status_code == 200 and demoted.json()["role"] == "member"
    assert members_of("acme")[cn.uids["cy"]] == "owner"
    changed = [e for e in events(audit_log) if e["event"] == "member_role_changed"]
    assert [(e["target"], e["role"], e["via"]) for e in changed] == [
        (cn.uids["cy"], "owner", "instance_admin"),
        (cn.uids["ben"], "member", "instance_admin"),
    ]
    assert (
        cn.client.patch(url(base + cn.uids["dee"]), json={"role": "member"}).json()[
            "code"
        ]
        == "not_member"
    )
    assert (
        cn.client.patch(url(base + cn.uids["cy"]), json={"role": "reader"}).json()[
            "code"
        ]
        == "bad_role"
    )


def test_orgs_promoting_a_password_account_to_owner_is_refused(
    cn: SimpleNamespace,
) -> None:
    with portal_db.get_session() as db:
        orgs_mod.add_member(
            db, org_id=cn.orgs["acme"], user_id=cn.ids["ada"], role="member"
        )
    be_ada(cn)
    response = cn.client.patch(
        url("/api/admin/orgs/acme/members/" + cn.uids["ada"]), json={"role": "owner"}
    )
    assert (response.status_code, response.json()["code"]) == (
        409,
        "github_account_required",
    )
    assert members_of("acme")[cn.uids["ada"]] == "member"


def test_orgs_promotion_drops_grants_and_removal_revokes_tokens(
    cn: SimpleNamespace, audit_log
) -> None:
    with portal_db.get_session() as db:
        db.add(
            ProjectGrant(
                org_id=cn.orgs["acme"],
                project_id=cn.projects["api"],
                user_id=cn.ids["cy"],
                role="viewer",
                granted_by=cn.ids["ben"],
                created_at=_iso(datetime.now(timezone.utc)),
            )
        )
    be_ada(cn)
    base = "/api/admin/orgs/acme/members/"
    up = cn.client.patch(url(base + cn.uids["cy"]), json={"role": "admin"})
    assert up.status_code == 200, up.text
    with portal_db.get_session() as db:
        assert db.exec(select(ProjectGrant.user_id)).all() == []
    gone = cn.client.delete(url(base + cn.uids["cy"]))
    assert gone.status_code == 204
    assert cn.uids["cy"] not in members_of("acme")
    (event,) = [e for e in events(audit_log) if e["event"] == "member_removed"]
    assert event["target"] == cn.uids["cy"] and event["via"] == "instance_admin"
    assert cn.client.delete(url(base + cn.uids["cy"])).json()["code"] == "not_member"


def test_orgs_member_and_rename_writes_work_on_a_suspended_org(
    cn: SimpleNamespace,
) -> None:
    """D33: the admin routes are not gated by suspension."""
    set_org("acme", suspended_at=_iso(datetime.now(timezone.utc)), suspended_reason="r")
    be_ada(cn)
    add = cn.client.post(
        url("/api/admin/orgs/acme/members"),
        json={"uid": cn.uids["eve"], "role": "member"},
    )
    assert add.status_code == 200, add.text
    change = cn.client.patch(
        url("/api/admin/orgs/acme/members/" + cn.uids["eve"]), json={"role": "admin"}
    )
    assert change.status_code == 200, change.text
    remove = cn.client.delete(url("/api/admin/orgs/acme/members/" + cn.uids["eve"]))
    assert remove.status_code == 204
    created = cn.client.post(
        url("/api/admin/orgs"),
        json={"slug": "newco", "name": "N", "owner_uid": cn.uids["eve"]},
    )
    assert created.status_code == 201


def test_orgs_writes_on_a_deleted_org_give_org_deleted(cn: SimpleNamespace) -> None:
    set_org("acme", deleted_at=_iso(datetime.now(timezone.utc)))
    be_ada(cn)
    members = "/api/admin/orgs/acme/members"
    answers = [
        cn.client.patch(url("/api/admin/orgs/acme"), json={"name": "X"}),
        cn.client.post(url(members), json={"uid": cn.uids["eve"], "role": "member"}),
        cn.client.patch(url(members + "/" + cn.uids["cy"]), json={"role": "admin"}),
        cn.client.delete(url(members + "/" + cn.uids["cy"])),
    ]
    for response in answers:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "org_deleted"
    assert org_row("acme").name == "Acme"
    assert members_of("acme")[cn.uids["cy"]] == "member"
    # Reads, Restore and the other admin reads keep working.
    assert cn.client.get(url("/api/admin/orgs/acme")).status_code == 200


def test_orgs_routes_refuse_a_non_admin_and_the_wrong_host(cn: SimpleNamespace) -> None:
    cn.client.cookies.clear()
    for method, path, body in (
        ("POST", "/api/admin/orgs", {"slug": "x", "name": "x", "owner_uid": "u"}),
        ("GET", "/api/admin/orgs/acme", None),
        ("PATCH", "/api/admin/orgs/acme", {"name": "x"}),
        ("POST", "/api/admin/orgs/acme/members", {"uid": "u", "role": "member"}),
        ("PATCH", "/api/admin/orgs/acme/members/u", {"role": "member"}),
        ("DELETE", "/api/admin/orgs/acme/members/u", None),
        ("GET", "/api/admin/audit.csv", None),
    ):
        response = cn.client.request(method, url(path), json=body)
        assert response.status_code == 401, (method, path)
        assert response.json()["code"] == ADMIN_LOGIN_REQUIRED


# ---------------------------------------------------------------------------
# Audit across orgs (plan 59 section 4.9)
# ---------------------------------------------------------------------------


def test_audit_defaults_to_every_org_and_scope_orgless_narrows_it(
    cn: SimpleNamespace,
) -> None:
    audit_event(event="org_renamed", org_id=cn.orgs["acme"], org_slug="acme")
    audit_event(event="org_renamed", org_id=cn.orgs["bravo"], org_slug="bravo")
    audit_event(event="github_signin", org_id=None)
    be_ada(cn)
    everything = cn.client.get(url("/api/admin/audit")).json()["events"]
    assert {(e["event"], e["org"]) for e in everything} >= {
        ("org_renamed", "acme"),
        ("org_renamed", "bravo"),
        ("github_signin", None),
    }
    orgless = cn.client.get(url("/api/admin/audit"), params={"scope": "orgless"})
    assert {e["org"] for e in orgless.json()["events"]} == {None}
    one = cn.client.get(url("/api/admin/audit"), params={"org": "bravo"}).json()
    assert {e["org"] for e in one["events"]} == {"bravo"}
    bad = cn.client.get(url("/api/admin/audit"), params={"scope": "nope"})
    assert bad.status_code == 422
    assert (
        cn.client.get(url("/api/admin/audit"), params={"from": "x"}).json()["code"]
        == "bad_date"
    )


def test_audit_labels_a_project_target_against_its_own_events_org(
    cn: SimpleNamespace,
) -> None:
    audit_event(
        event="project_removed", org_id=cn.orgs["acme"], org_slug="acme", target="api"
    )
    audit_event(
        event="project_removed", org_id=cn.orgs["bravo"], org_slug="bravo", target="lib"
    )
    # "api" is acme's project, not bravo's: no label in bravo's event.
    audit_event(
        event="project_removed", org_id=cn.orgs["bravo"], org_slug="bravo", target="api"
    )
    be_ada(cn)
    events_ = cn.client.get(url("/api/admin/audit")).json()["events"]
    labels = {(e["org"], e["target"]): e["target_label"] for e in events_}
    assert labels[("acme", "api")] == "Api project"
    assert labels[("bravo", "lib")] == "Lib project"
    assert labels[("bravo", "api")] is None


def test_audit_shows_a_deleted_users_real_identity(cn: SimpleNamespace) -> None:
    audit_event(
        event="member_added",
        org_id=cn.orgs["acme"],
        org_slug="acme",
        actor_id=cn.ids["cy"],
        actor_label="@cy",
        target=cn.uids["cy"],
        fields={"github_login": "cy"},
    )
    mark(cn.ids["cy"], deleted_at=_iso(datetime.now(timezone.utc)))
    be_ada(cn)
    (event,) = [
        e
        for e in cn.client.get(url("/api/admin/audit")).json()["events"]
        if e["event"] == "member_added"
    ]
    assert event["actor"]["label"] == "@cy"
    assert event["target_label"] == "Cy (@cy)"
    assert event["fields"]["github_login"] == "cy"


def test_audit_csv_streams_the_same_filters_unmasked(cn: SimpleNamespace) -> None:
    audit_event(event="org_renamed", org_id=cn.orgs["acme"], org_slug="acme")
    audit_event(
        event="member_added",
        org_id=cn.orgs["bravo"],
        org_slug="bravo",
        actor_id=cn.ids["cy"],
        actor_label="@cy",
    )
    mark(cn.ids["cy"], deleted_at=_iso(datetime.now(timezone.utc)))
    be_ada(cn)
    response = cn.client.get(url("/api/admin/audit.csv"))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert (
        'filename="whygraph-admin-audit.csv"' in response.headers["content-disposition"]
    )
    lines = response.text.splitlines()
    assert lines[0].split(",")[:3] == ["id", "created_at", "org"]
    body = "\n".join(lines[1:])
    assert "org_renamed" in body and "member_added" in body
    assert "@cy" in body and "[Deleted User]" not in body
    filtered = cn.client.get(url("/api/admin/audit.csv"), params={"org": "bravo"})
    assert "member_added" in filtered.text and "org_renamed" not in filtered.text
    bad = cn.client.get(url("/api/admin/audit.csv"), params={"to": "x"})
    assert bad.status_code == 422
