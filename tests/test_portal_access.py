"""Project access over HTTP: the org default, grants and Restricted (M2f-1).

Reuses ``test_portal_members.py``'s ``team`` fixture (a production portal
where Ben owns ``acme``) and adds two projects to ``acme``, ``api`` and
``notes``. The grant routes arrive in a later step, so grants, Restricted
and the org default are written straight into the database here; what is
under test is what each person then sees and may do (M2f-1 plan sections
4.2-4.4 and 4.10).
"""

# ruff: noqa: F811 -- pytest fixtures (`team`, `production_env`, ...) are imported

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    github_fake,
    make_repo,
    production_env,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_members import be, team, with_roles  # noqa: F401 -- `team`
from whygraph.portal import db as portal_db
from whygraph.portal.models import Organization, Project, ProjectGrant

ALL_PERMISSIONS = [
    "project.read",
    "project.chat",
    "project.scan",
    "project.scan_full",
    "project.configure",
    "project.setup",
    "project.access",
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
