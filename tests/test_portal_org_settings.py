"""Org settings, ownership transfer and project removal (M2f-1 sections 4.8, 6.2 #5).

Over :func:`test_portal_members.team` (Ben owns ``acme``): ``PATCH
/api/org`` renames the org and sets its default project role (a default of
``none`` revokes the tokens of members who lost access, at once); ``POST
/api/org/transfer`` makes another member the owner and the caller an admin
in one transaction; ``DELETE /api/projects/{slug}`` is ``org.remove_project``
and is audited as ``project_removed``. Memberships are made directly, so
these tests do not depend on the members routes.
"""

# ruff: noqa: F811 -- pytest fixtures are imported from other test modules

from __future__ import annotations

from types import SimpleNamespace

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
from test_portal_identity_routes import audit_log, events  # noqa: F401
from test_portal_members import be, team  # noqa: F401
from whygraph.portal import connections
from whygraph.portal import db as portal_db
from whygraph.portal.models import (
    ConnectionToken,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    User,
)
from whygraph.portal.orgs import add_member


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def acme(team: SimpleNamespace, production_env: SimpleNamespace) -> SimpleNamespace:
    """``team`` plus project ``api``; Cy an admin, Dee and Eve members of ``acme``."""
    t = team
    t.project_id = _insert_project(
        t.org_id, "api", "Api", make_repo(production_env.tmp, "api"), t.ids["ben"]
    )
    for name, role in (("cy", "admin"), ("dee", "member"), ("eve", "member")):
        join(t, name, role)
    return t


def join(t: SimpleNamespace, name: str, role: str) -> None:
    with portal_db.get_session() as session:
        add_member(session, org_id=t.org_id, user_id=t.ids[name], role=role)


def roles(t: SimpleNamespace) -> dict[str, str]:
    with portal_db.get_session() as session:
        rows = session.exec(
            select(User.github_login, Membership.role)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == t.org_id)
        ).all()
    return dict(rows)


def org_row(t: SimpleNamespace) -> tuple[str, str]:
    with portal_db.get_session() as session:
        org = session.get(Organization, t.org_id)
        return org.name, org.default_project_role


def patch(t: SimpleNamespace, **body):  # noqa: ANN201
    return t.client.patch(at("acme") + "/api/org", json=body)


def transfer(t: SimpleNamespace, uid: str, confirm: str = "acme"):  # noqa: ANN201
    return t.client.post(
        at("acme") + "/api/org/transfer",
        json={"user_uid": uid, "confirm_slug": confirm},
    )


def token_for(t: SimpleNamespace, name: str) -> str:
    with portal_db.get_session() as session:
        return connections.issue(
            session,
            user=session.get(User, t.ids[name]),
            project=session.get(Project, t.project_id),
            client_name="laptop",
        )


def revoked_reason(token: str) -> str | None:
    with portal_db.get_session() as session:
        return session.exec(
            select(ConnectionToken.revoked_reason).where(
                ConnectionToken.token_hash == connections.hash_token(token)
            )
        ).one()


# ---------------------------------------------------------------------------
# PATCH /api/org
# ---------------------------------------------------------------------------


def test_the_owner_renames_the_org(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    be(t, "ben")
    response = patch(t, name="  Acme Corp  ")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "slug": "acme",
        "name": "Acme Corp",
        "default_project_role": "contributor",
    }
    assert org_row(t) == ("Acme Corp", "contributor")
    (event,) = [e for e in events(audit_log) if e["event"] == "org_renamed"]
    assert (event["org_id"], event["org"], event["name"], event["previous"]) == (
        t.org_id,
        "acme",
        "Acme Corp",
        "Acme",
    )
    # Unchanged: no second event.
    assert patch(t, name="Acme Corp").status_code == 200
    assert len([e for e in events(audit_log) if e["event"] == "org_renamed"]) == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"name": "   "}, "bad_name"),
        ({"name": "x" * 201}, "bad_name"),
        ({"name": "Bell\x07"}, "bad_name"),
        ({"name": "Tab\tname"}, "bad_name"),
        ({"default_project_role": "admin"}, "bad_role"),
        ({"default_project_role": "owner"}, "bad_role"),
        ({}, None),
        ({"slug": "other"}, None),  # extra fields are refused; slugs are immutable
    ],
)
def test_a_bad_patch_is_422(
    acme: SimpleNamespace, body: dict, code: str | None
) -> None:
    t = acme
    be(t, "ben")
    response = patch(t, **body)
    assert response.status_code == 422, response.text
    if code is not None:
        assert response.json()["code"] == code
    assert org_row(t) == ("Acme", "contributor")


def test_a_name_of_200_characters_is_fine(acme: SimpleNamespace) -> None:
    be(acme, "ben")
    assert patch(acme, name="y" * 200).status_code == 200


def test_only_owners_change_org_settings(acme: SimpleNamespace) -> None:
    t = acme
    for name in ("cy", "dee", "ada"):  # admin, member, instance admin (reader)
        be(t, name)
        response = patch(t, name="Mine")
        assert response.status_code == 403, (name, response.text)
        assert response.json()["code"] == "forbidden"
    be(t, "finn")  # no member
    assert patch(t, name="Mine").status_code == 404
    assert org_row(t) == ("Acme", "contributor")


def test_default_none_revokes_lost_access_at_once(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    dee, cy = token_for(t, "dee"), token_for(t, "cy")
    with portal_db.get_session() as session:  # Eve keeps access by a grant
        session.add(
            ProjectGrant(
                org_id=t.org_id,
                project_id=t.project_id,
                user_id=t.ids["eve"],
                role="viewer",
            )
        )
    eve = token_for(t, "eve")
    be(t, "ben")
    response = patch(t, default_project_role="viewer")
    assert response.status_code == 200, response.text
    assert revoked_reason(dee) is None  # viewers may connect agents
    response = patch(t, default_project_role="none")
    assert response.status_code == 200, response.text
    assert response.json()["default_project_role"] == "none"
    assert revoked_reason(dee) == "project_access_removed"
    assert revoked_reason(cy) is None  # an org admin is admin on every project
    assert revoked_reason(eve) is None
    changes = [e for e in events(audit_log) if e["event"] == "org_default_role_changed"]
    assert [(e["previous"], e["role"], e["tokens_revoked"]) for e in changes] == [
        ("contributor", "viewer", 0),
        ("viewer", "none", 1),
    ]
    # Back to contributor: Dee's project is visible again, the token stays revoked.
    assert patch(t, default_project_role="contributor").status_code == 200
    be(t, "dee")
    assert t.client.get(at("acme") + "/api/projects/api").status_code == 200
    assert revoked_reason(dee) == "project_access_removed"


def test_default_none_hides_projects_from_members(acme: SimpleNamespace) -> None:
    t = acme
    be(t, "ben")
    assert patch(t, default_project_role="none").status_code == 200
    be(t, "dee")
    assert t.client.get(at("acme") + "/api/projects").json()["projects"] == []
    assert t.client.get(at("acme") + "/api/projects/api").status_code == 404


# ---------------------------------------------------------------------------
# POST /api/org/transfer
# ---------------------------------------------------------------------------


def test_the_owner_transfers_ownership(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    with portal_db.get_session() as session:  # a grant Dee must lose as owner
        session.add(
            ProjectGrant(
                org_id=t.org_id,
                project_id=t.project_id,
                user_id=t.ids["dee"],
                role="viewer",
            )
        )
    be(t, "ben")
    response = transfer(t, t.uids["dee"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["owner"]["uid"], body["owner"]["role"]) == (t.uids["dee"], "owner")
    assert (body["previous_owner"]["uid"], body["previous_owner"]["role"]) == (
        t.uids["ben"],
        "admin",
    )
    assert roles(t) == {"ben": "admin", "cy": "admin", "dee": "owner", "eve": "member"}
    with portal_db.get_session() as session:
        assert (
            session.exec(
                select(ProjectGrant).where(ProjectGrant.user_id == t.ids["dee"])
            ).all()
            == []
        )
    (event,) = [
        e for e in events(audit_log) if e["event"] == "org_ownership_transferred"
    ]
    assert (event["uid"], event["target"], event["org_id"], event["previous"]) == (
        t.uids["ben"],
        t.uids["dee"],
        t.org_id,
        "member",
    )
    # Ben is an admin now: no second transfer, no org settings.
    response = transfer(t, t.uids["cy"])
    assert response.status_code == 403
    assert response.json()["action"] == "org.own"
    be(t, "dee")
    assert patch(t, name="Dee's").status_code == 200


def test_transfer_refusals(acme: SimpleNamespace) -> None:
    t = acme
    be(t, "ben")
    response = transfer(t, t.uids["cy"], confirm="wrong")
    assert (response.status_code, response.json()["code"]) == (409, "confirm_slug")
    response = transfer(t, t.uids["ben"])
    assert (response.status_code, response.json()["code"]) == (409, "self_transfer")
    response = transfer(t, t.uids["finn"])  # signed in once, no member
    assert (response.status_code, response.json()["code"]) == (404, "not_member")
    response = transfer(t, "no-such-uid")
    assert (response.status_code, response.json()["code"]) == (404, "not_member")
    with portal_db.get_session() as session:
        session.get(Membership, (t.org_id, t.ids["cy"])).role = "owner"
        session.get(User, t.ids["eve"]).disabled_at = "2026-10-06T00:00:00+00:00"
    response = transfer(t, t.uids["cy"])
    assert (response.status_code, response.json()["code"]) == (409, "already_owner")
    response = transfer(t, t.uids["eve"])
    assert (response.status_code, response.json()["code"]) == (409, "user_disabled")
    assert roles(t) == {"ben": "owner", "cy": "owner", "dee": "member", "eve": "member"}


def test_only_owners_transfer(acme: SimpleNamespace) -> None:
    t = acme
    for name in ("cy", "dee", "ada"):
        be(t, name)
        response = transfer(t, t.uids["eve"])
        assert response.status_code == 403, (name, response.text)
    assert roles(t)["ben"] == "owner"


# ---------------------------------------------------------------------------
# DELETE /api/projects/{slug} (org.remove_project)
# ---------------------------------------------------------------------------


def test_removing_a_project_is_an_org_admin_action_and_audited(
    acme: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = acme
    url = at("acme") + "/api/projects/api"
    be(t, "dee")
    response = t.client.request("DELETE", url, json={"confirm_name": "Api"})
    assert response.status_code == 403, response.text
    assert response.json() == {
        "error": "your role (member) cannot org.remove_project",
        "code": "forbidden",
        "action": "org.remove_project",
    }
    be(t, "cy")
    response = t.client.request("DELETE", url, json={"confirm_name": "Api"})
    assert response.status_code == 200, response.text
    (event,) = [e for e in events(audit_log) if e["event"] == "project_removed"]
    assert (event["uid"], event["org_id"], event["org"], event["project"]) == (
        t.uids["cy"],
        t.org_id,
        "acme",
        "api",
    )
