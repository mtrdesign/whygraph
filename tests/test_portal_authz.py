"""Unit tests for org slug rules and role authorization (pure modules)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from whygraph.portal.authz import (
    ROLE_ACTIONS,
    ROLES,
    Action,
    OrgAccess,
    Role,
    allowed,
    authorize,
)
from whygraph.portal.deps import ApiError
from whygraph.portal.orgs import (
    BUILTIN_ORG_SLUG,
    NEVER_ORG_HOSTS,
    ORG_SLUG_SQL_CHECK,
    RESERVED_ORG_SLUGS,
    is_valid_org_slug,
    validate_org_slug,
)


@pytest.mark.parametrize(
    "slug", ["a", "acme", "a-b", "a1", "local", "0", "a" * 40, "a-" + "b" * 37 + "c"]
)
def test_valid_org_slugs(slug):
    assert is_valid_org_slug(slug)
    validate_org_slug(slug)


@pytest.mark.parametrize(
    "slug",
    ["", "a" * 41, "-a", "a-", "-", "Acme", "a_b", "a b", "acme\n", "a.b"],
)
def test_invalid_org_slugs(slug):
    assert not is_valid_org_slug(slug)
    with pytest.raises(ValueError, match="invalid"):
        validate_org_slug(slug)


@pytest.mark.parametrize("slug", ["www", "api", "mcp", "whygraph", "staging", "setup"])
def test_reserved_org_slugs_refused_at_creation_only(slug):
    # Lookups check the format only, so growing the list never strands an org.
    assert is_valid_org_slug(slug)
    with pytest.raises(ValueError, match="reserved"):
        validate_org_slug(slug)


def test_punycode_lookalike_refused_at_creation():
    assert is_valid_org_slug("xn--abc")
    with pytest.raises(ValueError, match="double dash"):
        validate_org_slug("xn--abc")
    validate_org_slug("a-b--c")  # only the 3rd-4th position is reserved


def test_builtin_slug_is_not_reserved():
    assert BUILTIN_ORG_SLUG not in RESERVED_ORG_SLUGS
    validate_org_slug(BUILTIN_ORG_SLUG)


def test_never_org_hosts_are_reserved():
    assert NEVER_ORG_HOSTS <= RESERVED_ORG_SLUGS
    assert isinstance(NEVER_ORG_HOSTS, frozenset)


def test_sql_check_mirrors_regex():
    assert "{0,38}" in ORG_SLUG_SQL_CHECK
    assert ORG_SLUG_SQL_CHECK.startswith("slug ~ ")


def test_roles():
    # The membership roles, explicit: the internal reader is never storable.
    assert ROLES == ("owner", "admin", "member")
    assert Role.READER.value not in ROLES
    assert set(ROLES) == {r.value for r in Role} - {"reader"}


def test_add_member_refuses_the_reader_role():
    from whygraph.portal.orgs import add_member

    with pytest.raises(ValueError, match="cannot be stored"):
        add_member(None, org_id=1, user_id=1, role=Role.READER)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        add_member(None, org_id=1, user_id=1, role="superuser")  # type: ignore[arg-type]


def test_role_actions_match_table():
    member = {"org.read", "project.read", "project.chat", "project.scan"}
    admin = member | {
        "org.add_project",
        "org.configure",
        "project.configure",
        "project.setup",
    }
    owner = admin | {"org.own"}
    # Every org action is an owner action; user.self / instance.admin are in
    # no role's set.
    assert {a.value for a in Action} == owner | {"user.self", "instance.admin"}
    assert {r: {a.value for a in s} for r, s in ROLE_ACTIONS.items()} == {
        Role.MEMBER: member,
        Role.ADMIN: admin,
        Role.OWNER: owner,
        Role.READER: {"org.read", "project.read"},
    }


def test_allowed():
    assert allowed(Role.MEMBER, Action.PROJECT_SCAN)
    assert not allowed(Role.MEMBER, Action.PROJECT_CONFIGURE)
    assert not allowed(Role.ADMIN, Action.ORG_OWN)
    assert allowed(Role.OWNER, Action.ORG_OWN)


def _access(role):
    return OrgAccess(org_id=1, org_slug="acme", org_name="Acme", role=role)


def test_authorize_member_forbidden():
    with pytest.raises(ApiError) as exc:
        authorize(_access(Role.MEMBER), Action.PROJECT_CONFIGURE)
    err = exc.value
    assert err.status == 403
    assert err.code == "forbidden"
    assert err.error == "your role (member) cannot project.configure"
    body = json.loads(err.response().body)
    assert body == {
        "error": "your role (member) cannot project.configure",
        "code": "forbidden",
        "action": "project.configure",
    }


def test_authorize_admin_passes():
    authorize(_access(Role.ADMIN), Action.PROJECT_CONFIGURE)
    authorize(_access(Role.MEMBER), Action.PROJECT_READ)


def test_authorize_org_mismatch_is_404():
    other = SimpleNamespace(org_id=2)
    with pytest.raises(ApiError) as exc:
        authorize(_access(Role.OWNER), Action.PROJECT_READ, other)
    assert exc.value.status == 404
    assert exc.value.error == "not found"
    authorize(_access(Role.MEMBER), Action.PROJECT_READ, SimpleNamespace(org_id=1))
