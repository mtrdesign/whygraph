"""Project access over HTTP: the org default, grants and Restricted (M2f-1).

Reuses ``test_portal_members.py``'s ``team`` fixture (a production portal
where Ben owns ``acme``) and adds two projects to ``acme``, ``api`` and
``notes``. The first tests write grants, Restricted and the org default
straight into the database and check what each person then sees and may do
(M2f-1 plan sections 4.2-4.4 and 4.10); the last ones drive the access routes
(section 4.8) and the members PATCH that clears grants and revokes tokens.
"""

# ruff: noqa: F811 -- pytest fixtures (`team`, `production_env`, ...) are imported

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    github_fake,
    make_repo,
    production_env,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_members import be, team, with_roles  # noqa: F401 -- `team`
from test_portal_identity_routes import audit_log, events  # noqa: F401 -- fixture
from test_portal_members import set_role
from whygraph.portal import connections
from whygraph.portal import db as portal_db
from whygraph.portal.models import (
    ConnectionToken,
    Organization,
    Project,
    ProjectGrant,
    User,
)

ALL_PERMISSIONS = [
    "project.read",
    "project.chat",
    "project.scan",
    "project.scan_full",
    "project.configure",
    "project.setup",
    "project.access",
    "project.usage",
]
CONTRIBUTOR_PERMISSIONS = ["project.read", "project.chat", "project.scan"]


@pytest.fixture
def acme(team: SimpleNamespace, production_env: SimpleNamespace) -> SimpleNamespace:
    """``team`` plus ``api`` and ``notes`` in ``acme``; Cy and Dee members, Eve admin."""
    t = team
    t.projects = {
        slug: _insert_project(
            t.org_id,
            slug,
            slug.title(),
            make_repo(production_env.tmp, slug),
            t.ids["ben"],
        )
        for slug in ("api", "notes")
    }
    with_roles(t, cy="member", dee="member", eve="admin")
    return t


def _grant(t: SimpleNamespace, name: str, slug: str, role: str | None) -> None:
    """Set (or, with ``None``, drop) ``name``'s grant on ``slug``."""
    key = (t.projects[slug], t.ids[name])
    with portal_db.get_session() as session:
        row = session.get(ProjectGrant, key)
        if role is None:
            if row is not None:
                session.delete(row)
            return
        if row is None:
            row = ProjectGrant(
                org_id=t.org_id,
                project_id=key[0],
                user_id=key[1],
                role=role,
                granted_by=t.ids["ben"],
            )
        row.role = role
        session.add(row)


def _restrict(t: SimpleNamespace, slug: str, restricted: bool = True) -> None:
    with portal_db.get_session() as session:
        session.get(Project, t.projects[slug]).restricted = restricted


def _default(t: SimpleNamespace, value: str) -> None:
    with portal_db.get_session() as session:
        session.get(Organization, t.org_id).default_project_role = value


def _listed(t: SimpleNamespace) -> dict[str, dict]:
    """``GET /api/projects`` as the current session, by slug."""
    response = t.client.get(at("acme") + "/api/projects")
    assert response.status_code == 200, response.text
    return {p["slug"]: p for p in response.json()["projects"]}


def _get(t: SimpleNamespace, path: str) -> httpx.Response:
    return t.client.get(at("acme") + path)


def _assert_unknown(response: httpx.Response, slug: str) -> None:
    """The very ``404`` an unknown slug gets: existence must not leak."""
    assert response.status_code == 404, response.text
    assert response.json() == {"error": f"project {slug!r} not found"}


def test_the_default_contributor_sees_every_project(acme: SimpleNamespace) -> None:
    t = acme
    be(t, "cy")
    listed = _listed(t)
    assert set(listed) == {"api", "notes"}
    for project in listed.values():
        assert project["restricted"] is False
        assert project["my_role"] == "contributor"
        assert project["permissions"] == CONTRIBUTOR_PERMISSIONS
    detail = _get(t, "/api/projects/api").json()
    assert (detail["my_role"], detail["permissions"]) == (
        "contributor",
        CONTRIBUTOR_PERMISSIONS,
    )
    state = _get(t, "/api/portal/state").json()
    assert state["org"]["default_project_role"] == "contributor"
    # Org owners and admins are project admins everywhere.
    for name in ("ben", "eve"):
        be(t, name)
        assert {p["my_role"] for p in _listed(t).values()} == {"admin"}
        assert _listed(t)["api"]["permissions"] == ALL_PERMISSIONS


def test_restricted_hides_the_project_from_members_without_a_grant(
    acme: SimpleNamespace,
) -> None:
    t = acme
    _restrict(t, "api")
    be(t, "cy")
    assert set(_listed(t)) == {"notes"}
    _assert_unknown(_get(t, "/api/projects/zzz"), "zzz")
    for path in (
        "/api/projects/api",
        "/api/projects/api/config",
        "/api/projects/api/scans",
        "/api/projects/api/tree",
        "/api/projects/api/chat/sessions",
    ):
        _assert_unknown(_get(t, path), "api")
    # Writes, and the org action that names the project, too.
    _assert_unknown(t.client.post(at("acme") + "/api/projects/api/scans"), "api")
    _assert_unknown(
        t.client.put(
            at("acme") + "/api/projects/api/config",
            json={"config": {"scan": {"forge": "auto"}}},
        ),
        "api",
    )
    _assert_unknown(
        t.client.request(
            "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "Api"}
        ),
        "api",
    )
    # Org owners and admins still see it, marked.
    for name in ("ben", "eve"):
        be(t, name)
        listed = _listed(t)
        assert set(listed) == {"api", "notes"}
        assert listed["api"]["restricted"] is True
        assert listed["api"]["my_role"] == "admin"
        assert _get(t, "/api/projects/api").status_code == 200


def test_a_grant_on_a_restricted_project_restores_it_with_its_role(
    acme: SimpleNamespace,
) -> None:
    t = acme
    _restrict(t, "api")
    _grant(t, "cy", "api", "viewer")
    be(t, "cy")
    listed = _listed(t)
    assert set(listed) == {"api", "notes"}
    assert (listed["api"]["my_role"], listed["api"]["permissions"]) == (
        "viewer",
        ["project.read"],
    )
    assert listed["notes"]["my_role"] == "contributor"
    assert _get(t, "/api/projects/api").status_code == 200
    refused = t.client.post(at("acme") + "/api/projects/api/scans")
    assert refused.status_code == 403, refused.text
    assert refused.json() == {
        "error": "your role on this project (viewer) cannot project.scan",
        "code": "forbidden",
        "action": "project.scan",
    }
    chat = _get(t, "/api/projects/api/chat/sessions")
    assert chat.status_code == 403 and chat.json()["action"] == "project.chat"
    # Dee, without a grant, still does not see it.
    be(t, "dee")
    assert set(_listed(t)) == {"notes"}
    _assert_unknown(_get(t, "/api/projects/api"), "api")
    # Dropping the grant hides it again on the next request.
    _grant(t, "cy", "api", None)
    be(t, "cy")
    _assert_unknown(_get(t, "/api/projects/api"), "api")


def test_the_default_none_shows_only_granted_projects(acme: SimpleNamespace) -> None:
    t = acme
    _default(t, "none")
    _grant(t, "dee", "notes", "contributor")
    be(t, "cy")
    assert _listed(t) == {}
    _assert_unknown(_get(t, "/api/projects/notes"), "notes")
    assert _get(t, "/api/portal/state").json()["org"]["default_project_role"] == "none"
    be(t, "dee")
    listed = _listed(t)
    assert set(listed) == {"notes"}
    assert listed["notes"]["my_role"] == "contributor"
    _assert_unknown(_get(t, "/api/projects/api"), "api")
    be(t, "ben")
    assert set(_listed(t)) == {"api", "notes"}


def test_the_default_viewer_and_grants_that_raise_or_lower(
    acme: SimpleNamespace,
) -> None:
    t = acme
    _default(t, "viewer")
    _grant(t, "cy", "api", "admin")
    be(t, "cy")
    listed = _listed(t)
    assert listed["notes"]["my_role"] == "viewer"
    assert listed["api"]["my_role"] == "admin"
    assert listed["api"]["permissions"] == ALL_PERMISSIONS
    # A project admin by grant configures the project...
    put = t.client.put(
        at("acme") + "/api/projects/api/config",
        json={"config": {"scan": {"forge": "auto"}}},
    )
    assert put.status_code == 200, put.text
    refused = t.client.put(
        at("acme") + "/api/projects/notes/config",
        json={"config": {"scan": {"forge": "auto"}}},
    )
    assert refused.status_code == 403
    assert refused.json()["error"] == (
        "your role on this project (viewer) cannot project.configure"
    )
    # ...but removing a project stays an org admin's action.
    removed = t.client.request(
        "DELETE", at("acme") + "/api/projects/api", json={"confirm_name": "Api"}
    )
    assert removed.status_code == 403, removed.text
    assert removed.json() == {
        "error": "your role (member) cannot org.remove_project",
        "code": "forbidden",
        "action": "org.remove_project",
    }
    # A grant lowers as well as raises.
    _default(t, "contributor")
    _grant(t, "cy", "notes", "viewer")
    assert _listed(t)["notes"]["my_role"] == "viewer"


def test_a_grant_for_an_org_admin_changes_nothing(acme: SimpleNamespace) -> None:
    """Org admins are project admins everywhere, Restricted included."""
    t = acme
    _restrict(t, "api")
    _grant(t, "eve", "api", "viewer")  # the routes refuse this; the rules ignore it
    be(t, "eve")
    assert _listed(t)["api"]["my_role"] == "admin"


def test_an_instance_admin_reads_every_project_as_a_viewer(
    acme: SimpleNamespace,
) -> None:
    t = acme
    _restrict(t, "api")
    _default(t, "none")
    be(t, "ada")
    listed = _listed(t)
    assert set(listed) == {"api", "notes"}
    assert {p["my_role"] for p in listed.values()} == {"viewer"}
    assert {tuple(p["permissions"]) for p in listed.values()} == {("project.read",)}
    assert _get(t, "/api/projects/api").status_code == 200


# ---------------------------------------------------------------------------
# The access routes (plan section 4.8)
# ---------------------------------------------------------------------------


def _access(t: SimpleNamespace, slug: str = "api") -> str:
    return at("acme") + f"/api/projects/{slug}/access"


def _put(t: SimpleNamespace, name: str, role: str, slug: str = "api") -> httpx.Response:
    return t.client.put(_access(t, slug) + f"/{t.uids[name]}", json={"role": role})


def _delete(t: SimpleNamespace, name: str, slug: str = "api") -> httpx.Response:
    return t.client.delete(_access(t, slug) + f"/{t.uids[name]}")


def _people(t: SimpleNamespace, slug: str = "api") -> dict[str, dict]:
    response = t.client.get(_access(t, slug))
    assert response.status_code == 200, response.text
    return {p["login"]: p for p in response.json()["people"]}


def _token(t: SimpleNamespace, name: str, slug: str = "api") -> str:
    with portal_db.get_session() as session:
        return connections.issue(
            session,
            user=session.get(User, t.ids[name]),
            project=session.get(Project, t.projects[slug]),
            client_name="laptop",
        )


def _revoked(token: str) -> str | None:
    with portal_db.get_session() as session:
        row = session.exec(
            select(ConnectionToken).where(
                ConnectionToken.token_hash == connections.hash_token(token)
            )
        ).one()
        return row.revoked_reason


def test_grant_change_and_remove(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    be(t, "ben")
    body = t.client.get(_access(t)).json()
    assert (body["restricted"], body["org_default"]) == (False, "contributor")
    assert body["invitations"] == []
    people = _people(t)
    assert people["ben"]["source"] == "org_admin"
    assert people["eve"]["project_role"] == "admin"
    assert (people["cy"]["project_role"], people["cy"]["source"]) == (
        "contributor",
        "default",
    )
    added = _put(t, "cy", "viewer")
    assert added.status_code == 200, added.text
    assert (added.json()["project_role"], added.json()["source"]) == ("viewer", "grant")
    assert _people(t)["cy"]["source"] == "grant"
    assert _put(t, "cy", "admin").status_code == 200
    assert _put(t, "cy", "admin").status_code == 200  # no change, no event
    be(t, "cy")
    assert _listed(t)["api"]["my_role"] == "admin"
    assert _listed(t)["notes"]["my_role"] == "contributor"
    be(t, "ben")
    assert _delete(t, "cy").status_code == 204
    assert _people(t)["cy"]["source"] == "default"
    records = [r for r in events(audit_log) if r["event"].startswith("project_grant")]
    assert [(r["event"], r.get("role"), r.get("previous")) for r in records] == [
        ("project_grant_added", "viewer", None),
        ("project_grant_changed", "admin", "viewer"),
        ("project_grant_removed", None, "admin"),
    ]
    assert {r["target"] for r in records} == {t.uids["cy"]}


def test_grant_refusals(acme: SimpleNamespace) -> None:
    t = acme
    be(t, "ben")
    for name in ("ben", "eve"):  # org owner and org admin
        refused = _put(t, name, "viewer")
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "org_admin"
    pat = _put(t, "pat", "viewer")  # a password account in no org
    assert (pat.status_code, pat.json()["code"]) == (404, "not_member")
    ghost = t.client.put(_access(t) + "/no-such-uid", json={"role": "viewer"})
    assert (ghost.status_code, ghost.json()["code"]) == (404, "not_member")
    assert _put(t, "cy", "owner").status_code == 422
    assert t.client.put(_access(t) + f"/{t.uids['cy']}", json={}).status_code == 422
    assert _delete(t, "cy").status_code == 404  # no grant to remove
    assert _delete(t, "pat").status_code == 404
    assert t.client.get(_access(t, "nope")).status_code == 404


def test_only_project_admins_manage_access(acme: SimpleNamespace) -> None:
    t = acme
    be(t, "cy")
    assert t.client.get(_access(t)).status_code == 403
    assert _put(t, "dee", "viewer").status_code == 403
    assert t.client.patch(_access(t), json={"restricted": True}).status_code == 403
    be(t, "ben")
    assert _put(t, "cy", "admin").status_code == 200
    be(t, "cy")  # a project admin by grant, on that project only
    assert _put(t, "dee", "viewer").status_code == 200
    assert _put(t, "dee", "viewer", "notes").status_code == 403
    be(t, "ada")  # an instance admin is a reader
    assert t.client.get(_access(t)).status_code == 403


def test_restricted_flow_with_grants_and_tokens(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    be(t, "ben")
    _put(t, "dee", "viewer", "notes")  # a grant elsewhere changes nothing here
    cy, dee, eve = _token(t, "cy"), _token(t, "dee"), _token(t, "eve")
    assert _put(t, "dee", "viewer").status_code == 200
    restricted = t.client.patch(_access(t), json={"restricted": True})
    assert (restricted.status_code, restricted.json()) == (200, {"restricted": True})
    # Cy has no grant: out at once, token revoked. Dee and the admins stay.
    assert _revoked(cy) == "project_access_removed"
    assert _revoked(dee) is None and _revoked(eve) is None
    be(t, "cy")
    _assert_unknown(_get(t, "/api/projects/api"), "api")
    assert "api" not in _listed(t)
    assert t.client.delete(at("acme") + "/api/projects/api").status_code == 404
    be(t, "dee")
    assert _listed(t)["api"]["my_role"] == "viewer"
    assert _listed(t)["api"]["restricted"] is True
    be(t, "ben")
    assert _people(t)["cy"]["project_role"] is None
    # A grant brings Cy back; removing it takes Dee out and revokes her token.
    assert _put(t, "cy", "contributor").status_code == 200
    be(t, "cy")
    assert _listed(t)["api"]["my_role"] == "contributor"
    be(t, "ben")
    assert _delete(t, "dee").status_code == 204
    assert _revoked(dee) == "project_access_removed"
    be(t, "dee")
    _assert_unknown(_get(t, "/api/projects/api"), "api")
    be(t, "ben")
    assert t.client.patch(_access(t), json={"restricted": False}).status_code == 200
    be(t, "dee")
    assert _listed(t)["api"]["my_role"] == "contributor"
    flips = [r for r in events(audit_log) if r["event"] == "project_restricted_changed"]
    assert [r["restricted"] for r in flips] == [True, False]


def test_the_default_none_shows_everyone_without_a_grant_as_no_access(
    acme: SimpleNamespace,
) -> None:
    t = acme
    _default(t, "none")
    be(t, "ben")
    people = _people(t)
    assert people["cy"]["project_role"] is None
    assert _put(t, "cy", "viewer").status_code == 200
    assert _people(t)["cy"]["project_role"] == "viewer"
    assert t.client.get(_access(t)).json()["org_default"] == "none"


def test_access_lists_open_invitations_with_a_grant_on_the_project(
    acme: SimpleNamespace,
) -> None:
    t = acme
    from datetime import datetime, timedelta, timezone

    from whygraph.portal.models import Invitation, InvitationGrant

    soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(
        timespec="seconds"
    )
    with portal_db.get_session() as session:
        invitation = Invitation(
            org_id=t.org_id,
            github_id=99001,
            github_login="newbie",
            role="member",
            invited_by=t.ids["ben"],
            expires_at=soon,
        )
        session.add(invitation)
        session.flush()
        session.add(
            InvitationGrant(
                invitation_id=invitation.id,
                org_id=t.org_id,
                project_id=t.projects["api"],
                role="viewer",
            )
        )
    be(t, "ben")
    (listed,) = t.client.get(_access(t)).json()["invitations"]
    assert (listed["github_login"], listed["role"]) == ("newbie", "viewer")
    assert t.client.get(_access(t, "notes")).json()["invitations"] == []


def test_promotion_deletes_grants_and_demotion_revokes_lost_access(
    acme: SimpleNamespace,
) -> None:
    t = acme
    be(t, "ben")
    assert _put(t, "cy", "viewer").status_code == 200
    assert _put(t, "cy", "viewer", "notes").status_code == 200
    assert set_role(t, "cy", "admin").status_code == 200
    with portal_db.get_session() as session:
        assert (
            session.exec(
                select(ProjectGrant).where(ProjectGrant.user_id == t.ids["cy"])
            ).all()
            == []
        )
    # An org admin keeps their own, other people's grants.
    assert _put(t, "dee", "viewer").status_code == 200
    assert _people(t)["cy"]["source"] == "org_admin"
    assert _people(t)["dee"]["source"] == "grant"
    # Demoting an admin of a Restricted project revokes the token at once.
    token = _token(t, "eve")
    assert t.client.patch(_access(t), json={"restricted": True}).status_code == 200
    assert _revoked(token) is None  # an org admin still sees it
    assert set_role(t, "eve", "member").status_code == 200
    assert _revoked(token) == "project_access_removed"
    # ... and one who keeps access keeps the token.
    keep = _token(t, "cy", "notes")
    assert set_role(t, "cy", "member").status_code == 200
    assert _revoked(keep) is None
    be(t, "eve")
    _assert_unknown(_get(t, "/api/projects/api"), "api")
