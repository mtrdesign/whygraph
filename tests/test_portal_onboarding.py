"""Onboarding, the welcome flag, the slug check, member grants, audit labels.

M2f-3 plan sections 4.12 and 6.2 #12-#15. Production cases run on the
``team`` fixture of ``test_portal_members`` (Ben owns ``acme``; Cy, Dee, Eve
and Finn have accounts and no org) and the ``world`` fixture of
``test_portal_github_import`` (a GitHub App on the fake); the local cases on
``test_portal_usage``'s ``local``.
"""

# ruff: noqa: F811 -- pytest fixtures are imported

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlmodel import col, select

from github_fake import FakeGitHub
from test_portal_app import (  # noqa: F401 -- fixtures
    at,
    env,
    github_fake,
    production_env,
)
from test_portal_github_import import (  # noqa: F401 -- fixtures
    World,
    app_env,
    connected,
    github_git_server,
    github_app_key,
    world,
)
from test_portal_members import (  # noqa: F401 -- fixtures
    add,
    be,
    invite,
    new_project,
    set_role,
    sign_in_as,
    team,
)
from test_portal_usage import local, stub_llm  # noqa: F401 -- fixtures
from whygraph.portal import db as portal_db
from whygraph.portal.models import (
    AgentCallDay,
    AuditEvent,
    ConnectionToken,
    Membership,
    Project,
    RetiredOrgSlug,
    User,
)
from whygraph.portal.secrets import LLM_API_KEY, put_secret
from whygraph.portal.throttle import Throttle


def items(response) -> dict[str, dict]:
    assert response.status_code == 200, response.text
    return {i["id"]: i for i in response.json()["items"]}


def onboarding(t: SimpleNamespace, org: str = "acme"):
    return t.client.get(at(org) + "/api/onboarding")


# ---------------------------------------------------------------------------
# Onboarding (6.2 #12)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from whygraph.portal.secrets import LLM_KEY_PROVIDERS

    for tag in LLM_KEY_PROVIDERS:
        monkeypatch.delenv(f"{tag.replace('-', '_').upper()}_API_KEY", raising=False)


def test_a_production_owner_sees_every_item_open(
    team: SimpleNamespace, no_env_keys: None
) -> None:
    be(team, "ben")
    response = onboarding(team)
    assert [i["id"] for i in response.json()["items"]] == [
        "github",
        "project",
        "llm_key",
        "invite",
        "agent",
    ]
    got = items(response)
    assert all(not i["done"] for i in got.values())
    assert all(set(i) == {"id", "done", "can_act"} for i in got.values())
    assert all(i["can_act"] for i in got.values())


def test_each_production_item_closes_on_its_own_rule(
    team: SimpleNamespace, production_env: SimpleNamespace, no_env_keys: None
) -> None:
    t = team
    be(t, "ben")
    project_id = new_project(t, production_env, "api")
    got = items(onboarding(t))
    assert got["project"]["done"] and not got["agent"]["done"]
    with portal_db.get_session() as session:
        put_secret(
            session,
            kind=LLM_API_KEY,
            value="sk-test-key",
            provider="openrouter",
            project_id=project_id,
            org_id=t.org_id,
        )
    assert items(onboarding(t))["llm_key"]["done"]
    # A connection token of the caller counts as the agent; another user's
    # token, or a revoked one, does not.
    with portal_db.get_session() as session:
        session.add(
            ConnectionToken(
                user_id=t.ids["cy"],
                org_id=t.org_id,
                project_id=project_id,
                token_hash="a" * 64,
                client_name="cy-laptop",
            )
        )
        session.add(
            ConnectionToken(
                user_id=t.ids["ben"],
                org_id=t.org_id,
                project_id=project_id,
                token_hash="b" * 64,
                client_name="old",
                revoked_at="2026-01-01T00:00:00+00:00",
                revoked_reason="user_revoked",
            )
        )
    assert not items(onboarding(t))["agent"]["done"]
    with portal_db.get_session() as session:
        session.exec(
            update(ConnectionToken)
            .where(col(ConnectionToken.client_name) == "old")
            .values(revoked_at=None, revoked_reason=None)
        )
    assert items(onboarding(t))["agent"]["done"]
    # Invitations and other members close "invite".
    assert not items(onboarding(t))["invite"]["done"]
    assert invite(t, "nofa").status_code == 201
    assert items(onboarding(t))["invite"]["done"]


def test_a_second_member_closes_invite_and_an_agent_row_closes_agent(
    team: SimpleNamespace, production_env: SimpleNamespace
) -> None:
    t = team
    be(t, "ben")
    project_id = new_project(t, production_env, "api")
    assert add(t, "cy", "admin").status_code == 201
    assert items(onboarding(t))["invite"]["done"]
    with portal_db.get_session() as session:
        session.add(
            AgentCallDay(
                org_id=t.org_id,
                project_id=project_id,
                day="2026-10-01",
                source="mcp",
                user_id=t.ids["cy"],
                kind="whygraph_evidence_for",
                calls=1,
            )
        )
    assert items(onboarding(t))["agent"]["done"]


def test_can_act_follows_the_role_and_a_member_is_refused(
    team: SimpleNamespace, no_env_keys: None
) -> None:
    t = team
    be(t, "ben")
    assert add(t, "cy", "admin").status_code == 201
    assert add(t, "dee", "member").status_code == 201
    be(t, "cy")
    got = items(onboarding(t))
    assert got["llm_key"]["can_act"] is False  # owners configure keys
    assert got["invite"]["can_act"] is True  # admins manage members
    assert got["project"]["can_act"] is True
    be(t, "dee")
    refused = onboarding(t)
    assert refused.status_code == 403
    assert refused.json()["action"] == "org.add_project"


def test_the_github_item_needs_a_project_or_a_listed_installation(
    world: World, no_env_keys: None
) -> None:
    w = world
    got = items(w.client.get(at("acme") + "/api/onboarding"))
    assert not got["github"]["done"] and got["github"]["can_act"]
    # A session token alone does not count: the listing decides.
    w.fake.uninstall(7)
    w.fake.uninstall(8)
    connected(w)
    assert not items(w.client.get(at("acme") + "/api/onboarding"))["github"]["done"]
    w.fake.add_installation(9, "ben")
    # Memoised for 60 s per session: the earlier answer is still served.
    assert not items(w.client.get(at("acme") + "/api/onboarding"))["github"]["done"]
    w.state.github_listing.clear()
    assert items(w.client.get(at("acme") + "/api/onboarding"))["github"]["done"]


def test_a_github_project_closes_the_github_item(
    team: SimpleNamespace, production_env: SimpleNamespace
) -> None:
    t = team
    be(t, "ben")
    new_project(t, production_env, "api")
    with portal_db.get_session() as session:
        session.exec(update(Project).values(source="local"))
    assert not items(onboarding(t))["github"]["done"]
    with portal_db.get_session() as session:
        session.exec(update(Project).values(source="github"))
    assert items(onboarding(t))["github"]["done"]


def test_a_local_portal_has_three_items_and_omits_the_key_for_linked_orgs(
    local: SimpleNamespace, no_env_keys: None
) -> None:
    w = local
    response = w.client.get("/api/onboarding")
    assert [i["id"] for i in response.json()["items"]] == [
        "llm_key",
        "project",
        "agent",
    ]
    got = items(response)
    assert got["project"]["done"] and not got["llm_key"]["done"]
    assert not got["agent"]["done"] and got["llm_key"]["can_act"]
    # An unflushed count is enough.
    w.state.agent_calls.add(
        org_id=w.org_id,
        project_id=w.project_id,
        source="mcp",
        user_id=w.user_id,
        connection_id=None,
        kind="whygraph_evidence_for",
    )
    assert items(w.client.get("/api/onboarding"))["agent"]["done"]
    with portal_db.get_session() as session:
        put_secret(
            session,
            kind=LLM_API_KEY,
            value="sk-test-key",
            provider="openrouter",
            org_id=w.org_id,
        )
    assert items(w.client.get("/api/onboarding"))["llm_key"]["done"]
    # Every project linked: the key item does not apply.
    with portal_db.get_session() as session:
        session.exec(update(Project).values(source="platform"))
    assert "llm_key" not in items(w.client.get("/api/onboarding"))


def test_an_environment_key_closes_the_key_item(
    local: SimpleNamespace, no_env_keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    assert items(local.client.get("/api/onboarding"))["llm_key"]["done"]


# ---------------------------------------------------------------------------
# Welcome (6.2 #13)
# ---------------------------------------------------------------------------


def flags(t: SimpleNamespace) -> dict[str, bool]:
    with portal_db.get_session() as session:
        rows = session.exec(
            select(User.github_login, Membership.welcome_pending)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == t.org_id)
        ).all()
    return dict(rows)


def state_of(t: SimpleNamespace, org: str = "acme") -> dict:
    return t.client.get(at(org) + "/api/portal/state").json()


def test_a_direct_add_sets_the_flag_but_org_creation_and_a_role_change_do_not(
    team: SimpleNamespace,
) -> None:
    t = team
    be(t, "ben")
    assert flags(t) == {"ben": False}  # the creator
    assert add(t, "cy").status_code == 201
    assert flags(t) == {"ben": False, "cy": True}
    assert state_of(t)["welcome"] is None
    be(t, "cy")
    assert state_of(t)["welcome"] == {"org_name": "Acme", "role": "member"}
    # The base host names no org: no welcome there.
    assert state_of(t, None).get("welcome") in (None,)
    be(t, "ben")
    assert set_role(t, "cy", "admin").status_code == 200
    assert flags(t)["cy"] is True  # a role change neither sets nor clears
    be(t, "cy")
    assert state_of(t)["welcome"] == {"org_name": "Acme", "role": "admin"}


def test_redemption_flags_only_the_membership_it_inserts(
    team: SimpleNamespace, github_fake: FakeGitHub
) -> None:
    t = team
    github_fake.add_user("gus")
    be(t, "ben")
    assert invite(t, "gus").status_code == 201
    assert "gus" not in flags(t)
    assert sign_in_as(t, "gus").status_code == 200
    assert flags(t)["gus"] is True
    assert state_of(t)["welcome"] == {"org_name": "Acme", "role": "member"}
    assert t.client.delete(at("acme") + "/api/org/welcome").status_code == 204
    assert flags(t)["gus"] is False


def test_dismissing_clears_only_the_callers_flag_and_is_idempotent(
    team: SimpleNamespace,
) -> None:
    t = team
    be(t, "ben")
    assert add(t, "cy").status_code == 201
    assert add(t, "dee").status_code == 201
    be(t, "cy")
    assert t.client.delete(at("acme") + "/api/org/welcome").status_code == 204
    assert t.client.delete(at("acme") + "/api/org/welcome").status_code == 204
    assert flags(t) == {"ben": False, "cy": False, "dee": True}
    assert state_of(t)["welcome"] is None
    # No membership in the org: the same refusal as any org route.
    be(t, "eve")
    assert t.client.delete(at("acme") + "/api/org/welcome").status_code == 404


def test_account_orgs_rows_say_which_org_is_new(team: SimpleNamespace) -> None:
    t = team
    be(t, "ben")
    assert add(t, "cy").status_code == 201
    be(t, "cy")
    rows = t.client.get(at() + "/api/account/orgs").json()
    assert [(r["slug"], r["new"]) for r in rows] == [("acme", True)]
    assert t.client.delete(at("acme") + "/api/org/welcome").status_code == 204
    assert t.client.get(at() + "/api/account/orgs").json()[0]["new"] is False


# ---------------------------------------------------------------------------
# Slug check (6.2 #14)
# ---------------------------------------------------------------------------


def check(t: SimpleNamespace, slug: str, org: str | None = None):
    return t.client.get(at(org) + "/api/orgs/slug-check", params={"slug": slug})


def test_the_slug_check_names_the_reason(team: SimpleNamespace) -> None:
    t = team
    be(t, "cy")
    with portal_db.get_session() as session:
        session.add(RetiredOrgSlug(slug="gone"))
    cases = {
        "fresh-team": (True, None),
        "Bad_Slug": (False, "invalid"),
        "ab--cd": (False, "invalid"),
        "": (False, "invalid"),
        "admin": (False, "reserved"),
        "acme": (False, "taken"),
        "gone": (False, "taken"),
    }
    for slug, (available, reason) in cases.items():
        response = check(t, slug)
        assert response.status_code == 200, (slug, response.text)
        assert response.json() == {
            "slug": slug,
            "available": available,
            "reason": reason,
        }


def test_the_slug_check_is_base_host_only_and_needs_a_session(
    team: SimpleNamespace,
) -> None:
    t = team
    be(t, "ben")
    assert check(t, "fresh", "acme").status_code == 404
    t.client.cookies.clear()
    assert check(t, "fresh").status_code == 401


def test_the_slug_check_is_throttled_per_user(team: SimpleNamespace) -> None:
    t = team
    t.client.app.state.portal.slug_check = Throttle(2, 60)
    be(t, "cy")
    assert check(t, "one").status_code == 200
    assert check(t, "two").status_code == 200
    third = check(t, "three")
    assert third.status_code == 429
    assert third.json()["code"] == "throttled" and "Retry-After" in third.headers
    be(t, "dee")  # another user has their own budget
    assert check(t, "three").status_code == 200


# ---------------------------------------------------------------------------
# Members grants and audit labels (6.2 #15)
# ---------------------------------------------------------------------------


def test_grants_reach_org_members_holders_only(
    team: SimpleNamespace, production_env: SimpleNamespace
) -> None:
    t = team
    new_project(t, production_env, "api")
    be(t, "ben")
    assert (
        invite(t, "cy", grants=[{"project": "api", "role": "viewer"}]).status_code
        == 201
    )
    assert add(t, "dee", "admin").status_code == 201
    assert add(t, "eve", "member").status_code == 201
    expected = [{"project": "api", "name": "Api", "role": "viewer"}]
    for who in ("ben", "dee"):  # an owner and an admin
        be(t, who)
        rows = {
            m["github_login"]: m
            for m in t.client.get(at("acme") + "/api/org/members").json()
        }
        assert rows["cy"]["grants"] == expected, who
        assert rows["eve"]["grants"] == [] and rows["ben"]["grants"] == []
    be(t, "eve")  # a member lists the people, never their grants
    rows = t.client.get(at("acme") + "/api/org/members").json()
    assert rows and all("grants" not in m for m in rows)
    assert "viewer" not in t.client.get(at("acme") + "/api/org/members").text


def test_audit_rows_carry_a_target_label(
    team: SimpleNamespace, production_env: SimpleNamespace
) -> None:
    t = team
    new_project(t, production_env, "api")
    be(t, "ben")
    with portal_db.get_session() as session:
        session.add(
            AuditEvent(
                created_at="2026-10-01T00:00:00+00:00",
                org_id=t.org_id,
                org_slug="acme",
                event="member_removed",
                target=t.uids["cy"],
            )
        )
        session.add(
            AuditEvent(
                created_at="2026-10-01T00:00:01+00:00",
                org_id=t.org_id,
                org_slug="acme",
                event="access_changed",
                target="api",
            )
        )
        session.add(
            AuditEvent(
                created_at="2026-10-01T00:00:02+00:00",
                org_id=t.org_id,
                org_slug="acme",
                event="x",
                target="gone-project",
            )
        )
        session.add(
            AuditEvent(
                created_at="2026-10-01T00:00:03+00:00",
                org_id=t.org_id,
                org_slug="acme",
                event="y",
            )
        )
    rows = t.client.get(at("acme") + "/api/org/audit").json()["events"]
    labels = {
        r["event"]: r["target_label"]
        for r in rows
        if r["event"] in ("x", "y", "member_removed", "access_changed")
    }
    assert labels == {
        "member_removed": "Cy (@cy)",
        "access_changed": "Api",
        "x": None,
        "y": None,
    }
