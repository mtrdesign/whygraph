"""Org members over HTTP (M2d-1 plan sections 4.5 and 5.3).

The ``team`` fixture is a claimed production portal (Ada, the password
instance admin) where Ben signed in with GitHub and created ``acme``; Cy,
Dee, Eve and Finn signed in once (so they can be added) and belong to no org
yet, and Pat is a password account. Each test builds the roles it needs
through the members routes themselves. Who the next request is changes with
:func:`be`, which starts a real session row directly (cheaper than a GitHub
round trip, and the GitHub sign-in throttle stays out of the way).
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`, ...) are imported

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import select

from github_fake import FakeGitHub
from test_portal_app import (  # noqa: F401 -- fixtures
    PRODUCTION_ORG_ROUTES,
    _filled,
    at,
    claim_instance,
    env,
    github_fake,
    github_sign_in,
    make_repo,
    password_user,
    portal_client,
    prod_portal,
    production_env,
    signed_in,
)
from test_portal_hosts_isolation import _insert_project
from test_portal_identity_routes import (  # noqa: F401 -- `audit_log` is a fixture
    audit_log,
    create_org,
    events,
)
from whygraph.portal import db as portal_db
from whygraph.portal import member_routes
from whygraph.portal import sessions
from whygraph.portal.member_routes import NO_SUCH_GITHUB_USER
from whygraph.portal.github_auth import GitHubRateLimited
from whygraph.portal.models import (
    Invitation,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    User,
)
from whygraph.portal.throttle import Throttle

NOT_FOUND = {"error": "not found"}
READER_ONLY_READS = (
    "instance admins can only read an organization they are not a member of"
)
MEMBER_FIELDS = {
    "uid",
    "display_name",
    "github_login",
    "avatar_url",
    "role",
    "joined_at",
    "disabled",
}
NEWCOMERS = ("cy", "dee", "eve", "finn")
"""GitHub accounts that signed in once and belong to no org."""


# ---------------------------------------------------------------------------
# Fixture and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def team(github_fake: FakeGitHub) -> Iterator[SimpleNamespace]:
    """Ada (instance admin), Ben (owner of ``acme``), four newcomers and Pat."""
    for login in ("eve", "finn"):
        github_fake.add_user(login)
    with prod_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        assert github_sign_in(client, "ben").status_code == 200
        assert create_org(client, "acme", "Acme").status_code == 201
        for login in NEWCOMERS:
            assert github_sign_in(client, login).status_code == 200
        client.cookies.clear()
        password_user("pat@example.com")
        ids, uids = {}, {}
        with portal_db.get_session() as session:
            people = [("ada", User.email == "ada@example.com")]
            people += [("pat", User.email == "pat@example.com")]
            people += [(n, User.github_login == n) for n in ("ben", *NEWCOMERS)]
            for name, where in people:
                user = session.exec(select(User).where(where)).one()
                ids[name], uids[name] = user.id, user.uid
            org_id = session.exec(
                select(Organization.id).where(Organization.slug == "acme")
            ).one()
        yield SimpleNamespace(client=client, ids=ids, uids=uids, org_id=org_id)


def be(t: SimpleNamespace, name: str) -> str:
    """Make ``name`` the client's one session; return its token."""
    t.client.cookies.clear()
    return signed_in(t.client, t.ids[name])


def add(
    t: SimpleNamespace, login: str, role: str = "member", org: str = "acme"
) -> httpx.Response:
    return t.client.post(
        at(org) + "/api/org/members", json={"github_login": login, "role": role}
    )


def set_role(t: SimpleNamespace, name: str, role: str) -> httpx.Response:
    return t.client.patch(
        at("acme") + f"/api/org/members/{t.uids[name]}", json={"role": role}
    )


def remove(t: SimpleNamespace, name: str) -> httpx.Response:
    return t.client.delete(at("acme") + f"/api/org/members/{t.uids[name]}")


def leave(t: SimpleNamespace, org: str = "acme") -> httpx.Response:
    return t.client.delete(at(org) + "/api/org/membership")


def roles(t: SimpleNamespace) -> dict[str, str]:
    """``{github_login: role}`` from the database (no session needed)."""
    with portal_db.get_session() as session:
        rows = session.exec(
            select(User.github_login, Membership.role)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == t.org_id)
        ).all()
    return dict(rows)


def assert_code(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.text


def assert_forbidden(response: httpx.Response, role: str, action: str) -> None:
    assert response.status_code == 403, response.text
    assert response.json() == {
        "error": f"your role ({role}) cannot {action}",
        "code": "forbidden",
        "action": action,
    }


def with_roles(t: SimpleNamespace, **wanted: str) -> None:
    """As Ben (an owner), add each newcomer at the role given."""
    be(t, "ben")
    for login, role in wanted.items():
        assert add(t, login, role).status_code == 201, (login, role)


# ---------------------------------------------------------------------------
# Add, list, re-role, remove
# ---------------------------------------------------------------------------


def test_an_owner_adds_lists_re_roles_and_removes(team: SimpleNamespace) -> None:
    t = team
    be(t, "ben")
    added = add(t, "cy")
    assert added.status_code == 201, added.text
    row = added.json()
    assert set(row) == MEMBER_FIELDS
    assert row["uid"] == t.uids["cy"]
    assert (row["github_login"], row["role"], row["disabled"]) == (
        "cy",
        "member",
        False,
    )
    assert row["display_name"] == "Cy"
    assert row["avatar_url"].startswith("https://avatars.example.test/u/")
    assert row["joined_at"]
    listed = t.client.get(at("acme") + "/api/org/members")
    assert listed.status_code == 200
    body = listed.json()
    assert [(m["github_login"], m["role"]) for m in body] == [
        ("ben", "owner"),
        ("cy", "member"),
    ]
    assert all(set(m) == MEMBER_FIELDS for m in body)  # never an email
    changed = set_role(t, "cy", "admin")
    assert changed.status_code == 200 and changed.json()["role"] == "admin"
    assert remove(t, "cy").status_code == 204
    assert roles(t) == {"ben": "owner"}


def test_a_login_is_trimmed_and_found_case_insensitively(
    team: SimpleNamespace,
) -> None:
    t = team
    be(t, "ben")
    assert add(t, "  @cy ").json()["github_login"] == "cy"
    assert add(t, "Dee").json()["github_login"] == "dee"
    assert roles(t) == {"ben": "owner", "cy": "member", "dee": "member"}


@pytest.mark.parametrize(
    "login", ["ben--x", "a" * 40, "-ben", "ben-", "", "be n", "@@ben", "b_en"]
)
def test_a_malformed_login_is_422_bad_login(team: SimpleNamespace, login: str) -> None:
    be(team, "ben")
    assert_code(add(team, login), 422, "bad_login")


def test_the_longest_valid_login_is_looked_up(team: SimpleNamespace) -> None:
    be(team, "ben")
    assert_code(add(team, "a" * 39), 404, "no_such_github_user")
    assert_code(add(team, "a-b-c"), 404, "no_such_github_user")


@pytest.mark.parametrize("role", ["reader", "superuser", "Owner", ""])
def test_a_role_a_membership_cannot_hold_is_422_bad_role(
    team: SimpleNamespace, role: str
) -> None:
    t = team
    be(t, "ben")
    assert_code(add(t, "cy", role), 422, "bad_role")
    assert add(t, "cy").status_code == 201
    assert_code(set_role(t, "cy", role), 422, "bad_role")


def test_someone_who_never_signed_in_is_invited_and_github_strangers_are_404(
    team: SimpleNamespace,
) -> None:
    """M2f-1: no "must have signed in" refusal; GitHub decides who exists."""
    t = team
    be(t, "ben")
    invited = add(t, "nofa")  # on GitHub, refused at sign-in (no 2FA): no account
    assert invited.status_code == 201, invited.text
    assert invited.json()["pending"] is True
    assert "nofa" not in roles(t)
    for login in ("nobody", "pat"):  # not on GitHub; pat has a password only
        response = add(t, login)
        assert response.status_code == 404, (login, response.text)
        assert response.json() == {
            "error": NO_SUCH_GITHUB_USER,
            "code": "no_such_github_user",
        }


def test_an_existing_member_is_409_already_member(team: SimpleNamespace) -> None:
    t = team
    be(t, "ben")
    assert add(t, "cy").status_code == 201
    assert_code(add(t, "cy", "admin"), 409, "already_member")
    assert_code(add(t, "ben"), 409, "already_member")
    assert roles(t)["cy"] == "member"


def test_a_disabled_account_is_409_user_disabled(team: SimpleNamespace) -> None:
    t = team
    be(t, "ada")
    disabled = t.client.patch(
        at() + f"/api/admin/users/{t.uids['dee']}", json={"disabled": True}
    )
    assert disabled.status_code == 200, disabled.text
    be(t, "ben")
    assert_code(add(t, "dee"), 409, "user_disabled")
    assert "dee" not in roles(t)


def test_unknown_and_other_org_uids_are_404_not_member(team: SimpleNamespace) -> None:
    t = team
    be(t, "ben")
    for response in (
        t.client.patch(
            at("acme") + "/api/org/members/no-such-uid", json={"role": "admin"}
        ),
        t.client.delete(at("acme") + "/api/org/members/no-such-uid"),
        set_role(t, "cy", "admin"),  # signed in, but in no org
        remove(t, "cy"),
    ):
        assert_code(response, 404, "not_member")


# ---------------------------------------------------------------------------
# The owner rules (plan section 0.2 #11)
# ---------------------------------------------------------------------------


def test_an_admin_manages_members_and_admins_but_never_owners(
    team: SimpleNamespace,
) -> None:
    t = team
    with_roles(t, cy="admin", dee="member")
    be(t, "cy")
    # Adds members and admins, never an owner.
    assert add(t, "eve", "admin").status_code == 201
    assert_code(add(t, "finn", "owner"), 403, "owner_required")
    assert "finn" not in roles(t)
    # Re-roles members and admins, never to owner.
    assert set_role(t, "eve", "member").json()["role"] == "member"
    assert set_role(t, "eve", "admin").json()["role"] == "admin"
    assert set_role(t, "dee", "admin").json()["role"] == "admin"
    assert_code(set_role(t, "dee", "owner"), 403, "owner_required")
    # Never changes or removes an owner, even to the same role.
    for role in ("admin", "member", "owner"):
        assert_code(set_role(t, "ben", role), 403, "owner_required")
    assert_code(remove(t, "ben"), 403, "owner_required")
    # Removes admins and members.
    assert remove(t, "eve").status_code == 204
    assert set_role(t, "dee", "member").status_code == 200
    assert remove(t, "dee").status_code == 204
    assert roles(t) == {"ben": "owner", "cy": "admin"}


def test_an_admin_may_demote_themselves(team: SimpleNamespace) -> None:
    t = team
    with_roles(t, cy="admin")
    be(t, "cy")
    demoted = set_role(t, "cy", "member")
    assert demoted.status_code == 200 and demoted.json()["role"] == "member"
    # ... and is a member from the next request on.
    assert_forbidden(add(t, "dee"), "member", "org.members")


def test_an_owner_does_all_of_it(team: SimpleNamespace) -> None:
    t = team
    be(t, "ben")
    assert add(t, "finn", "owner").json()["role"] == "owner"
    assert add(t, "cy", "admin").status_code == 201
    assert set_role(t, "finn", "admin").json()["role"] == "admin"
    assert set_role(t, "finn", "owner").json()["role"] == "owner"
    assert set_role(t, "cy", "owner").json()["role"] == "owner"
    assert remove(t, "cy").status_code == 204  # removes an owner
    assert set_role(t, "finn", "member").status_code == 200  # demotes one
    assert roles(t) == {"ben": "owner", "finn": "member"}


def test_a_member_may_list_and_leave_and_nothing_else(team: SimpleNamespace) -> None:
    t = team
    with_roles(t, dee="member", cy="admin")
    be(t, "dee")
    listed = t.client.get(at("acme") + "/api/org/members")
    assert listed.status_code == 200 and len(listed.json()) == 3
    assert_forbidden(add(t, "eve"), "member", "org.members")
    assert_forbidden(set_role(t, "cy", "member"), "member", "org.members")
    assert_forbidden(set_role(t, "dee", "admin"), "member", "org.members")
    assert_forbidden(remove(t, "cy"), "member", "org.members")
    assert leave(t).status_code == 204
    assert "dee" not in roles(t)
    # A removed member's next request is the org's 404.
    gone = t.client.get(at("acme") + "/api/org/members")
    assert gone.status_code == 404 and gone.json() == NOT_FOUND


def test_a_removed_member_is_404_on_the_next_request(team: SimpleNamespace) -> None:
    t = team
    with_roles(t, dee="admin")
    dee = be(t, "dee")
    assert t.client.get(at("acme") + "/api/projects").status_code == 200
    be(t, "ben")
    assert remove(t, "dee").status_code == 204
    t.client.cookies.clear()
    signed_in_again = {"Cookie": f"{sessions.COOKIE_NAME}={dee}"}
    for path in ("/api/projects", "/api/org/members", "/api/portal/defaults"):
        refused = t.client.get(at("acme") + path, headers=signed_in_again)
        assert refused.status_code == 404 and refused.json() == NOT_FOUND, path


def test_an_instance_admin_reads_the_members_and_changes_nothing(
    team: SimpleNamespace,
) -> None:
    t = team
    with_roles(t, cy="member")
    be(t, "ada")  # no membership: reader
    listed = t.client.get(at("acme") + "/api/org/members")
    assert listed.status_code == 200
    assert [m["github_login"] for m in listed.json()] == ["ben", "cy"]
    for response in (
        add(t, "dee"),
        set_role(t, "cy", "admin"),
        remove(t, "cy"),
        leave(t),
    ):
        assert response.status_code == 403, response.text
        assert response.json() == {"error": READER_ONLY_READS, "code": "forbidden"}
    assert roles(t) == {"ben": "owner", "cy": "member"}


# ---------------------------------------------------------------------------
# Org settings are the owner's; project keys the admin's (plan section 0.1)
# ---------------------------------------------------------------------------


def test_only_an_owner_changes_the_org_settings_and_admins_set_project_keys(
    team: SimpleNamespace, production_env: SimpleNamespace
) -> None:
    t = team
    root = make_repo(production_env.tmp, "api")
    _insert_project(t.org_id, "api", "Acme API", root, t.ids["ben"])
    with_roles(t, cy="admin")
    defaults = {
        "config": {"llm": {"model": "anthropic/acme-model"}},
        "secrets": {"llm": {"anthropic": "sk-acme-org-1111"}},
    }
    project_key = {"secrets": {"llm": {"openai": "sk-acme-project-2222"}}}
    for name, defaults_status in (("cy", 403), ("ben", 200)):
        be(t, name)
        put = t.client.put(at("acme") + "/api/portal/defaults", json=defaults)
        assert put.status_code == defaults_status, (name, put.text)
        if defaults_status == 403:
            assert_forbidden(put, "admin", "org.configure")
        config = t.client.put(at("acme") + "/api/projects/api/config", json=project_key)
        assert config.status_code == 200, (name, config.text)
        assert config.json()["secrets"]["llm"]["openai"]["set"] is True
        # Both read the org settings.
        assert t.client.get(at("acme") + "/api/portal/defaults").status_code == 200
    be(t, "ben")
    saved = t.client.get(at("acme") + "/api/portal/defaults").json()
    assert saved["config"]["llm"]["model"] == "anthropic/acme-model"


# ---------------------------------------------------------------------------
# The last owner (plan section 4.5)
# ---------------------------------------------------------------------------


def test_the_last_owner_cannot_be_demoted_removed_or_leave(
    team: SimpleNamespace,
) -> None:
    t = team
    with_roles(t, cy="admin")
    be(t, "ben")
    for role in ("admin", "member"):
        assert_code(set_role(t, "ben", role), 409, "last_owner")
    assert_code(remove(t, "ben"), 409, "last_owner")
    assert_code(leave(t), 409, "last_owner")
    assert set_role(t, "ben", "owner").status_code == 200  # unchanged is fine
    assert roles(t) == {"ben": "owner", "cy": "admin"}
    # With a second owner, the first may leave.
    assert set_role(t, "cy", "owner").status_code == 200
    assert leave(t).status_code == 204
    assert roles(t) == {"cy": "owner"}


def _race(
    t: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, calls: list[tuple]
) -> list[httpx.Response]:
    """Run ``calls`` (``(actor, method, path, body)``) at once, each its own session.

    The owner rows are held a moment after they are locked, so without
    the lock every call would count the owners before any committed.
    """
    lock_owners = member_routes._lock_owners

    def slow(db, org_id):  # noqa: ANN001, ANN202
        owners = lock_owners(db, org_id)
        time.sleep(0.4)
        return owners

    monkeypatch.setattr(member_routes, "_lock_owners", slow)
    tokens = []
    with portal_db.get_session() as session:
        for actor, *_ in calls:
            tokens.append(sessions.create_session(session, t.ids[actor], "pytest"))
    t.client.cookies.clear()
    barrier = threading.Barrier(len(calls))
    results: list[httpx.Response | None] = [None] * len(calls)

    def run(index: int) -> None:
        _, method, path, body = calls[index]
        barrier.wait()
        results[index] = t.client.request(
            method,
            at("acme") + path,
            json=body,
            headers={"Cookie": f"{sessions.COOKIE_NAME}={tokens[index]}"},
        )

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(r is not None for r in results)
    return results  # type: ignore[return-value]


def test_two_concurrent_self_demotions_leave_one_owner(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = team
    with_roles(t, finn="owner")
    results = _race(
        t,
        monkeypatch,
        [
            (name, "PATCH", f"/api/org/members/{t.uids[name]}", {"role": "admin"})
            for name in ("ben", "finn")
        ],
    )
    assert sorted(r.status_code for r in results) == [200, 409], [
        r.text for r in results
    ]
    (refused,) = (r for r in results if r.status_code == 409)
    assert refused.json()["code"] == "last_owner"
    assert list(roles(t).values()).count("owner") == 1


def test_two_owners_demoting_each_other_at_once_leave_one_owner(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loser was demoted meanwhile: it is no owner by the time it runs."""
    t = team
    with_roles(t, finn="owner")
    results = _race(
        t,
        monkeypatch,
        [
            ("ben", "PATCH", f"/api/org/members/{t.uids['finn']}", {"role": "admin"}),
            ("finn", "PATCH", f"/api/org/members/{t.uids['ben']}", {"role": "admin"}),
        ],
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses[0] == 200 and statuses[1] in (403, 409), [r.text for r in results]
    assert list(roles(t).values()).count("owner") == 1


def test_two_owners_leaving_at_once_leave_one_owner(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = team
    with_roles(t, finn="owner")
    results = _race(
        t,
        monkeypatch,
        [(name, "DELETE", "/api/org/membership", None) for name in ("ben", "finn")],
    )
    assert sorted(r.status_code for r in results) == [204, 409]
    assert list(roles(t).values()) == ["owner"]


# ---------------------------------------------------------------------------
# Throttle, audit, local mode
# ---------------------------------------------------------------------------


def test_adding_is_throttled_per_org(
    team: SimpleNamespace, github_fake: FakeGitHub
) -> None:
    t = team
    t.client.app.state.portal.member_add_org = Throttle(3, 3600)
    be(t, "ben")
    assert create_org(t.client, "bravo", "Bravo").status_code == 201
    assert add(t, "cy").status_code == 201
    assert add(t, "nobody").status_code == 404  # a miss on GitHub counts too
    assert add(t, "cy").status_code == 409
    asked = len(github_fake.user_lookups)
    throttled = add(t, "dee")
    assert_code(throttled, 429, "throttled")
    assert int(throttled.headers["Retry-After"]) > 0
    assert len(github_fake.user_lookups) == asked  # refused before GitHub
    assert "dee" not in roles(t)
    assert add(t, "dee", org="bravo").status_code == 201  # another org's bucket


def test_every_member_change_is_audited(
    team: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = team
    be(t, "ben")
    audit_log.clear()
    add(t, "nobody")
    add(t, "@@x")
    add(t, "cy", "superuser")
    add(t, "cy", "admin")
    add(t, "cy")
    set_role(t, "cy", "member")
    set_role(t, "cy", "member")  # unchanged: no event
    add(t, "finn", "owner")
    remove(t, "cy")
    leave(t)
    records = [r for r in events(audit_log) if r["event"].startswith("member_")]
    assert [(r["event"], r.get("reason")) for r in records] == [
        ("member_add_refused", "no_such_github_user"),
        ("member_add_refused", "bad_login"),
        ("member_add_refused", "bad_role"),
        ("member_added", None),
        ("member_add_refused", "already_member"),
        ("member_role_changed", None),
        ("member_added", None),
        ("member_removed", None),
        ("member_left", None),
    ]
    assert all(r["uid"] == t.uids["ben"] and r["org"] == "acme" for r in records)
    assert records[0]["github_login"] == "nobody"
    assert records[1]["github_login"] == "'@@x'"
    assert records[3]["target"] == t.uids["cy"] and records[3]["role"] == "admin"
    assert (records[5]["role"], records[5]["previous"]) == ("member", "admin")
    assert records[7]["target"] == t.uids["cy"]
    assert records[8]["role"] == "owner"


def test_an_admin_adding_an_owner_is_audited_as_refused(
    team: SimpleNamespace, audit_log: pytest.LogCaptureFixture
) -> None:
    t = team
    with_roles(t, cy="admin")
    be(t, "cy")
    audit_log.clear()
    add(t, "dee", "owner")
    (record,) = [r for r in events(audit_log) if r["event"] == "member_add_refused"]
    assert (record["reason"], record["github_login"]) == ("owner_required", "dee")


def test_every_members_route_is_404_in_local_mode(env: SimpleNamespace) -> None:
    with portal_client() as client:
        assert (
            client.post("/api/portal/setup", json={"display_name": "Tess"}).status_code
            == 201
        )
        for path, method in sorted(PRODUCTION_ORG_ROUTES):
            response = client.request(
                method, _filled(path), json={"github_login": "ben", "role": "member"}
            )
            assert response.status_code == 404, (method, path, response.text)
            assert response.json() == NOT_FOUND, (method, path)


# ---------------------------------------------------------------------------
# Invitations (M2f-1 plan sections 4.8 and 6.2 #4)
# ---------------------------------------------------------------------------


def invite(
    t: SimpleNamespace,
    login: str,
    role: str = "member",
    grants: list[dict] | None = None,
    org: str = "acme",
) -> httpx.Response:
    body: dict = {"github_login": login, "role": role}
    if grants is not None:
        body["grants"] = grants
    return t.client.post(at(org) + "/api/org/members", json=body)


def invitations(t: SimpleNamespace, org: str = "acme") -> list[dict]:
    response = t.client.get(at(org) + "/api/org/invitations")
    assert response.status_code == 200, response.text
    return response.json()


def revoke_invitation(
    t: SimpleNamespace, uid: str, org: str = "acme"
) -> httpx.Response:
    return t.client.delete(at(org) + f"/api/org/invitations/{uid}")


def new_project(t: SimpleNamespace, env: SimpleNamespace, slug: str) -> int:
    root = make_repo(env.tmp, slug)
    return _insert_project(t.org_id, slug, slug.title(), root, t.ids["ben"])


def org_id_of(slug: str) -> int:
    with portal_db.get_session() as session:
        return session.exec(
            select(Organization.id).where(Organization.slug == slug)
        ).one()


def membership_of(login: str) -> dict[str, str]:
    """``{org slug: role}`` of the account holding ``login``."""
    with portal_db.get_session() as session:
        rows = session.exec(
            select(Organization.slug, Membership.role)
            .join(Membership, Membership.org_id == Organization.id)
            .join(User, User.id == Membership.user_id)
            .where(User.github_login == login)
        ).all()
    return dict(rows)


def grants_of(login: str) -> dict[str, str]:
    """``{project slug: project role}`` of the account holding ``login``."""
    with portal_db.get_session() as session:
        rows = session.exec(
            select(Project.slug, ProjectGrant.role)
            .join(ProjectGrant, ProjectGrant.project_id == Project.id)
            .join(User, User.id == ProjectGrant.user_id)
            .where(User.github_login == login)
        ).all()
    return dict(rows)


def sign_in_as(t: SimpleNamespace, login: str) -> httpx.Response:
    t.client.cookies.clear()
    return github_sign_in(t.client, login)


def test_someone_without_an_account_is_invited_with_grants(
    team: SimpleNamespace,
    github_fake: FakeGitHub,
    production_env: SimpleNamespace,
    audit_log: pytest.LogCaptureFixture,
) -> None:
    t = team
    new_project(t, production_env, "api")
    gus = github_fake.add_user("gus")
    be(t, "ben")
    audit_log.clear()
    response = invite(t, "@Gus", grants=[{"project": "api", "role": "viewer"}])
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["pending"] is True
    assert (row["github_login"], row["role"], row["status"]) == (
        "gus",
        "member",
        "open",
    )
    assert row["grants"] == [{"project": "api", "role": "viewer"}]
    assert row["invited_by"]["uid"] == t.uids["ben"]
    created = datetime.fromisoformat(row["created_at"])
    assert datetime.fromisoformat(row["expires_at"]) - created == timedelta(days=14)
    # Resolved on GitHub with the OAuth App's client credentials.
    assert github_fake.user_lookups[-1] == ("Gus", True)
    with portal_db.get_session() as session:
        stored = session.exec(select(Invitation)).one()
        assert (stored.github_id, stored.org_id) == (gus.id, t.org_id)
    assert "gus" not in roles(t)
    listed = invitations(t)
    assert [i["uid"] for i in listed] == [row["uid"]]
    assert listed[0] == {k: v for k, v in row.items() if k != "pending"}
    (record,) = [r for r in events(audit_log) if r["event"] == "invitation_created"]
    assert (record["uid"], record["org"], record["invitation"]) == (
        t.uids["ben"],
        "acme",
        row["uid"],
    )
    assert (record["github_login"], record["role"], record["grants"]) == (
        "gus",
        "member",
        1,
    )


def test_an_existing_account_is_added_at_once_with_its_grants(
    team: SimpleNamespace, production_env: SimpleNamespace
) -> None:
    t = team
    new_project(t, production_env, "api")
    be(t, "ben")
    added = invite(t, "cy", grants=[{"project": "api", "role": "contributor"}])
    assert added.status_code == 201, added.text
    assert set(added.json()) == MEMBER_FIELDS  # the bare member row, as before
    assert roles(t)["cy"] == "member"
    assert grants_of("cy") == {"api": "contributor"}
    with portal_db.get_session() as session:
        grant = session.exec(select(ProjectGrant)).one()
        assert grant.granted_by == t.ids["ben"]
    assert invitations(t) == []


def test_github_decides_who_exists(
    team: SimpleNamespace, github_fake: FakeGitHub
) -> None:
    t = team
    github_fake.add_user("acme-corp", account_type="Organization")
    be(t, "ben")
    for login in ("nobody", "acme-corp"):
        assert_code(invite(t, login), 404, "no_such_github_user")
    github_fake.force("users", status=500)
    assert_code(invite(t, "dee"), 502, "github_unavailable")
    github_fake.force("users", exc=httpx.ReadTimeout("slow"))
    assert_code(invite(t, "dee"), 502, "github_unavailable")
    assert roles(t) == {"ben": "owner"}
    assert invitations(t) == []


def test_github_rate_limits_are_503_with_retry_after(
    team: SimpleNamespace, github_fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = team
    state = t.client.app.state.portal
    be(t, "ben")
    # The OAuth App's credentials refused: one anonymous retry, under the
    # instance-wide budget.
    state.github_lookup = Throttle(1, 3600)
    github_fake.client_secret = "rotated-elsewhere"
    assert invite(t, "dee").status_code == 201
    assert github_fake.user_lookups[-2:] == [("dee", False), ("dee", False)]
    refused = invite(t, "eve")
    assert_code(refused, 503, "github_rate_limited")
    assert int(refused.headers["Retry-After"]) > 0
    assert github_fake.user_lookups[-1] == ("eve", False)  # no anonymous retry

    def limited(login: str, **_: object) -> None:
        raise GitHubRateLimited(42)

    monkeypatch.setattr(state.github, "user_by_login", limited)
    refused = invite(t, "finn")
    assert_code(refused, 503, "github_rate_limited")
    assert refused.headers["Retry-After"] == "42"


def test_an_open_invitation_is_409_and_an_expired_one_is_replaced(
    team: SimpleNamespace, github_fake: FakeGitHub
) -> None:
    t = team
    github_fake.add_user("gus")
    be(t, "ben")
    first = invite(t, "gus")
    assert first.status_code == 201
    assert_code(invite(t, "gus", "admin"), 409, "already_invited")
    with portal_db.get_session() as session:
        stored = session.exec(select(Invitation)).one()
        stored.expires_at = "2020-01-01T00:00:00+00:00"
        session.add(stored)
    assert [i["status"] for i in invitations(t)] == ["expired"]
    second = invite(t, "gus", "admin")
    assert second.status_code == 201, second.text
    listed = {i["uid"]: i for i in invitations(t)}
    assert listed[first.json()["uid"]]["status"] == "revoked"
    assert listed[second.json()["uid"]]["status"] == "open"
    assert listed[second.json()["uid"]]["role"] == "admin"


@pytest.mark.parametrize(
    "role, grants, status, code",
    [
        ("admin", [{"project": "api", "role": "viewer"}], 422, "grants_for_org_admin"),
        ("owner", [{"project": "api", "role": "admin"}], 422, "grants_for_org_admin"),
        ("member", [{"project": "api", "role": "owner"}], 422, "bad_role"),
        ("member", [{"project": "nope", "role": "viewer"}], 404, "no_such_project"),
        ("member", [{"project": "web", "role": "viewer"}], 404, "no_such_project"),
        (
            "member",
            [{"project": "api", "role": "viewer"}, {"project": "api", "role": "admin"}],
            422,
            "duplicate_grant",
        ),
    ],
)
def test_grants_are_checked(
    team: SimpleNamespace,
    github_fake: FakeGitHub,
    production_env: SimpleNamespace,
    role: str,
    grants: list[dict],
    status: int,
    code: str,
) -> None:
    t = team
    new_project(t, production_env, "api")
    github_fake.add_user("gus")
    be(t, "ben")
    assert create_org(t.client, "bravo", "Bravo").status_code == 201
    web = make_repo(production_env.tmp, "web")  # bravo's project, not acme's
    _insert_project(org_id_of("bravo"), "web", "Web", web, t.ids["ben"])
    for login in ("gus", "cy"):  # an invitation and an existing account alike
        assert_code(invite(t, login, role, grants), status, code)
    assert invitations(t) == []
    assert roles(t) == {"ben": "owner"}


def test_revoking_an_invitation(
    team: SimpleNamespace,
    github_fake: FakeGitHub,
    audit_log: pytest.LogCaptureFixture,
) -> None:
    t = team
    for login in ("gus", "hal"):
        github_fake.add_user(login)
    with_roles(t, cy="admin", dee="member")
    gus = invite(t, "gus").json()["uid"]
    hal = invite(t, "hal", "owner").json()["uid"]
    assert create_org(t.client, "bravo", "Bravo").status_code == 201
    other = invite(t, "gus", org="bravo").json()["uid"]
    be(t, "dee")  # a member: no invitations at all
    assert_forbidden(
        t.client.get(at("acme") + "/api/org/invitations"), "member", "org.members"
    )
    assert_forbidden(revoke_invitation(t, gus), "member", "org.members")
    be(t, "cy")
    assert len(invitations(t)) == 2
    assert_code(revoke_invitation(t, hal), 403, "owner_required")
    audit_log.clear()
    assert revoke_invitation(t, gus).status_code == 204
    assert_code(revoke_invitation(t, gus), 409, "invitation_closed")
    assert_code(revoke_invitation(t, "no-such-uid"), 404, "no_such_invitation")
    assert_code(revoke_invitation(t, other), 404, "no_such_invitation")  # bravo's
    (record,) = [r for r in events(audit_log) if r["event"] == "invitation_revoked"]
    assert (record["uid"], record["org"], record["invitation"]) == (
        t.uids["cy"],
        "acme",
        gus,
    )
    be(t, "ben")
    assert revoke_invitation(t, hal).status_code == 204
    assert {i["uid"]: i["status"] for i in invitations(t)} == {
        gus: "revoked",
        hal: "revoked",
    }
    # A revoked invitation is never redeemed.
    assert sign_in_as(t, "gus").status_code == 200
    assert membership_of("gus") == {"bravo": "member"}


def test_signing_in_redeems_every_open_invitation(
    team: SimpleNamespace,
    github_fake: FakeGitHub,
    production_env: SimpleNamespace,
    audit_log: pytest.LogCaptureFixture,
) -> None:
    t = team
    new_project(t, production_env, "api")
    github_fake.add_user("gus")
    be(t, "ben")
    acme = invite(t, "gus", grants=[{"project": "api", "role": "viewer"}]).json()
    assert create_org(t.client, "bravo", "Bravo").status_code == 201
    bravo = invite(t, "gus", "admin", org="bravo").json()
    audit_log.clear()
    assert sign_in_as(t, "gus").status_code == 200
    assert membership_of("gus") == {"acme": "member", "bravo": "admin"}
    assert grants_of("gus") == {"api": "viewer"}
    with portal_db.get_session() as session:
        gus_id = session.exec(select(User.id).where(User.github_login == "gus")).one()
        for invitation in session.exec(select(Invitation)).all():
            assert invitation.redeemed_by == gus_id and invitation.redeemed_at
        granted = session.exec(select(ProjectGrant)).one()
        assert granted.granted_by == t.ids["ben"]  # the inviter
    joined = [r for r in events(audit_log) if r["event"] == "member_joined_by_invite"]
    assert sorted((r["org"], r["role"], r["invitation"]) for r in joined) == [
        ("acme", "member", acme["uid"]),
        ("bravo", "admin", bravo["uid"]),
    ]
    signed = [r for r in events(audit_log) if r["event"] == "github_signin"]
    assert all(r["uid"] == signed[0]["uid"] for r in joined)
    # The next sign-in redeems nothing more.
    audit_log.clear()
    assert sign_in_as(t, "gus").status_code == 200
    assert not [r for r in events(audit_log) if r["event"] == "member_joined_by_invite"]
    be(t, "ben")
    assert [i["status"] for i in invitations(t)] == ["redeemed"]


def test_a_stale_stored_login_is_never_matched(
    team: SimpleNamespace, github_fake: FakeGitHub
) -> None:
    """Eve renamed on GitHub; a stranger now owns "eve" (plan section 0.2 #8)."""
    t = team
    github_fake.rename_user("eve", "eve-old")
    stranger = github_fake.add_user("eve")
    be(t, "ben")
    # The stored row still says "eve" (it refreshes on sign-in only), but
    # GitHub's id for "eve" is the stranger's: an invitation, not old Eve.
    response = invite(t, "eve")
    assert response.status_code == 201, response.text
    assert response.json()["pending"] is True
    assert "eve" not in roles(t)
    with portal_db.get_session() as session:
        assert session.exec(select(Invitation.github_id)).one() == stranger.id
    assert sign_in_as(t, "eve-old").status_code == 200  # old Eve: nothing
    assert membership_of("eve-old") == {}
    assert sign_in_as(t, "eve").status_code == 200  # the stranger redeems it
    assert membership_of("eve") == {"acme": "member"}


def test_a_deleted_project_or_org_drops_out_of_an_invitation(
    team: SimpleNamespace, github_fake: FakeGitHub, production_env: SimpleNamespace
) -> None:
    t = team
    new_project(t, production_env, "api")
    web = new_project(t, production_env, "web")
    github_fake.add_user("gus")
    be(t, "ben")
    grants = [
        {"project": "api", "role": "viewer"},
        {"project": "web", "role": "contributor"},
    ]
    assert invite(t, "gus", grants=grants).status_code == 201
    assert create_org(t.client, "bravo", "Bravo").status_code == 201
    assert invite(t, "gus", org="bravo").status_code == 201
    bravo_id = org_id_of("bravo")
    with portal_db.get_session() as session:
        session.delete(session.get(Project, web))
        session.delete(session.get(Organization, bravo_id))
    with portal_db.get_session() as session:
        assert (
            session.exec(select(Invitation).where(Invitation.org_id == bravo_id)).all()
            == []
        )
    assert sign_in_as(t, "gus").status_code == 200
    assert membership_of("gus") == {"acme": "member"}
    assert grants_of("gus") == {"api": "viewer"}


def test_a_disabled_account_redeems_nothing(
    team: SimpleNamespace, github_fake: FakeGitHub
) -> None:
    t = team
    gus = github_fake.add_user("gus")
    be(t, "ben")
    assert invite(t, "gus").status_code == 201
    with portal_db.get_session() as session:
        session.add(
            User(
                display_name="Gus",
                github_id=gus.id,
                github_login="gus",
                disabled_at="2026-01-01T00:00:00+00:00",
            )
        )
    assert_code(sign_in_as(t, "gus"), 403, "account_disabled")
    assert membership_of("gus") == {}
    be(t, "ben")
    assert [i["status"] for i in invitations(t)] == ["open"]


def test_a_failed_redemption_skips_that_invitation_not_the_sign_in(
    team: SimpleNamespace, github_fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = team
    github_fake.add_user("gus")
    be(t, "ben")
    assert invite(t, "gus").status_code == 201
    assert create_org(t.client, "bravo", "Bravo").status_code == 201
    assert invite(t, "gus", org="bravo").status_code == 201
    redeem_one = member_routes._redeem_one

    def failing(db, invitation_id, org_id, role, **kwargs):  # noqa: ANN001, ANN202
        if org_id == t.org_id:
            db.exec(text("SELECT 1 / 0"))  # a real database error
        return redeem_one(db, invitation_id, org_id, role, **kwargs)

    monkeypatch.setattr(member_routes, "_redeem_one", failing)
    assert sign_in_as(t, "gus").status_code == 200
    assert membership_of("gus") == {"bravo": "member"}
    be(t, "ben")
    assert [i["status"] for i in invitations(t)] == ["open"]
