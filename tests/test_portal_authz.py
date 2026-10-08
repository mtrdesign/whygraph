"""Unit tests for org slug rules and role authorization (pure modules)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from whygraph.portal.authz import (
    DEFAULT_PROJECT_ROLES,
    PROJECT_ACTIONS,
    PROJECT_ROLE_ACTIONS,
    PROJECT_ROLES,
    ROLE_ACTIONS,
    ROLES,
    Action,
    OrgAccess,
    ProjectRole,
    Role,
    allowed,
    authorize,
    effective_project_role,
    project_allowed,
)
from whygraph.portal.deps import ApiError, org_access
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
    # Org roles hold org actions only (M2f-1 plan section 4.2).
    member = {"org.read"}
    # An instance-admin reader reads usage too (M2f-2 plan section 4.9).
    reader = member | {"org.usage"}
    admin = reader | {
        "org.add_project",
        "org.remove_project",
        "org.members",
        "org.budgets",
    }
    # Only an owner changes the org settings (and org-level keys): M2d-1
    # moved org.configure from admin to owner (plan section 0.1).
    owner = admin | {"org.configure", "org.own", "org.audit"}
    assert {r: {a.value for a in s} for r, s in ROLE_ACTIONS.items()} == {
        Role.MEMBER: member,
        Role.ADMIN: admin,
        Role.OWNER: owner,
        Role.READER: reader,
    }
    viewer = {"project.read"}
    contributor = viewer | {"project.chat", "project.scan"}
    project_admin = contributor | {
        "project.scan_full",
        "project.configure",
        "project.setup",
        "project.access",
        "project.usage",
    }
    assert {r: {a.value for a in s} for r, s in PROJECT_ROLE_ACTIONS.items()} == {
        ProjectRole.VIEWER: viewer,
        ProjectRole.CONTRIBUTOR: contributor,
        ProjectRole.ADMIN: project_admin,
    }


def test_every_action_is_in_exactly_one_table():
    # Every action is an owner org action or a project admin action;
    # user.self / instance.admin are in no role's set.
    assert {a.value for a in Action} == {
        str(a)
        for a in ROLE_ACTIONS[Role.OWNER] | PROJECT_ROLE_ACTIONS[ProjectRole.ADMIN]
    } | {"user.self", "instance.admin"}
    assert PROJECT_ACTIONS == PROJECT_ROLE_ACTIONS[ProjectRole.ADMIN]
    for actions in ROLE_ACTIONS.values():
        assert not actions & PROJECT_ACTIONS
    for actions in PROJECT_ROLE_ACTIONS.values():
        assert actions <= PROJECT_ACTIONS
    # The stored values and the enums agree.
    assert PROJECT_ROLES == tuple(r.value for r in ProjectRole)
    assert set(DEFAULT_PROJECT_ROLES) == {"contributor", "viewer", "none"}


def test_allowed():
    assert allowed(Role.MEMBER, Action.ORG_READ)
    assert not allowed(Role.MEMBER, Action.ORG_ADD_PROJECT)
    assert not allowed(Role.MEMBER, Action.ORG_REMOVE_PROJECT)
    assert allowed(Role.ADMIN, Action.ORG_REMOVE_PROJECT)
    assert not allowed(Role.ADMIN, Action.ORG_OWN)
    assert allowed(Role.OWNER, Action.ORG_OWN)
    assert allowed(Role.ADMIN, Action.ORG_MEMBERS)
    assert not allowed(Role.MEMBER, Action.ORG_MEMBERS)
    assert not allowed(Role.ADMIN, Action.ORG_CONFIGURE)
    assert allowed(Role.OWNER, Action.ORG_CONFIGURE)
    assert not allowed(Role.ADMIN, Action.ORG_AUDIT)
    assert allowed(Role.OWNER, Action.ORG_AUDIT)
    assert allowed(Role.READER, Action.ORG_USAGE)
    assert not allowed(Role.MEMBER, Action.ORG_USAGE)
    assert allowed(Role.ADMIN, Action.ORG_BUDGETS)
    assert not allowed(Role.READER, Action.ORG_BUDGETS)
    # A project action is never an org role's.
    assert not allowed(Role.OWNER, Action.PROJECT_READ)


def test_project_allowed():
    assert project_allowed(ProjectRole.VIEWER, Action.PROJECT_READ)
    assert not project_allowed(ProjectRole.VIEWER, Action.PROJECT_CHAT)
    assert project_allowed(ProjectRole.CONTRIBUTOR, Action.PROJECT_SCAN)
    assert not project_allowed(ProjectRole.CONTRIBUTOR, Action.PROJECT_SCAN_FULL)
    assert project_allowed(ProjectRole.ADMIN, Action.PROJECT_ACCESS)
    assert project_allowed(ProjectRole.ADMIN, Action.PROJECT_USAGE)
    assert not project_allowed(ProjectRole.CONTRIBUTOR, Action.PROJECT_USAGE)
    assert not project_allowed(ProjectRole.ADMIN, Action.ORG_READ)


def test_the_two_role_enums_cannot_be_mixed_up():
    # Both are StrEnums sharing "admin": equal values, distinct types.
    assert Role.ADMIN == ProjectRole.ADMIN
    with pytest.raises(TypeError):
        allowed(ProjectRole.ADMIN, Action.ORG_READ)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        allowed("admin", Action.ORG_READ)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        project_allowed(Role.ADMIN, Action.PROJECT_READ)  # type: ignore[arg-type]


def _expected_role(org_role, default, restricted, grant):
    """The rules of M2f-1 plan section 0.2 #2-#4, #19, written out longhand."""
    if org_role in (Role.OWNER, Role.ADMIN):
        return ProjectRole.ADMIN
    if org_role == Role.READER:
        return ProjectRole.VIEWER
    if grant is not None:
        return grant  # a grant replaces the default, lower or higher
    if restricted:
        return None
    return {"contributor": ProjectRole.CONTRIBUTOR, "viewer": ProjectRole.VIEWER}.get(
        default
    )


@pytest.mark.parametrize("org_role", list(Role))
@pytest.mark.parametrize("grant", [None, *ProjectRole])
@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("default", DEFAULT_PROJECT_ROLES)
def test_effective_project_role_table(org_role, grant, restricted, default):
    assert effective_project_role(
        org_role, default, restricted, grant
    ) == _expected_role(org_role, default, restricted, grant)


def test_effective_project_role_spot_checks():
    member = Role.MEMBER
    assert effective_project_role(member, "contributor", False, None) is (
        ProjectRole.CONTRIBUTOR
    )
    assert effective_project_role(member, "none", False, None) is None
    assert effective_project_role(member, "contributor", True, None) is None
    # A grant lowers as well as raises, and opens a Restricted project.
    assert effective_project_role(member, "contributor", False, ProjectRole.VIEWER) is (
        ProjectRole.VIEWER
    )
    assert effective_project_role(member, "none", True, ProjectRole.ADMIN) is (
        ProjectRole.ADMIN
    )
    # Org admins and owners are project admins everywhere; a reader views.
    assert effective_project_role(Role.ADMIN, "none", True, ProjectRole.VIEWER) is (
        ProjectRole.ADMIN
    )
    assert effective_project_role(Role.READER, "none", True, None) is (
        ProjectRole.VIEWER
    )


def _access(role):
    return OrgAccess(
        org_id=1,
        org_slug="acme",
        org_name="Acme",
        role=role,
        user_id=7,
        default_project_role="contributor",
    )


_PROJECT = SimpleNamespace(org_id=1)


def test_authorize_org_action_forbidden():
    with pytest.raises(ApiError) as exc:
        authorize(_access(Role.MEMBER), Action.ORG_ADD_PROJECT)
    err = exc.value
    assert err.status == 403
    assert err.code == "forbidden"
    assert err.error == "your role (member) cannot org.add_project"
    body = json.loads(err.response().body)
    assert body == {
        "error": "your role (member) cannot org.add_project",
        "code": "forbidden",
        "action": "org.add_project",
    }


def test_authorize_project_action_forbidden():
    with pytest.raises(ApiError) as exc:
        authorize(
            _access(Role.MEMBER),
            Action.PROJECT_SCAN_FULL,
            _PROJECT,
            project_role=ProjectRole.CONTRIBUTOR,
        )
    err = exc.value
    assert err.status == 403
    assert json.loads(err.response().body) == {
        "error": "your role on this project (contributor) cannot project.scan_full",
        "code": "forbidden",
        "action": "project.scan_full",
    }


def test_authorize_passes():
    authorize(_access(Role.ADMIN), Action.ORG_ADD_PROJECT)
    authorize(_access(Role.MEMBER), Action.ORG_READ)
    authorize(
        _access(Role.MEMBER),
        Action.PROJECT_READ,
        _PROJECT,
        project_role=ProjectRole.VIEWER,
    )
    authorize(
        _access(Role.MEMBER),
        Action.PROJECT_CONFIGURE,
        _PROJECT,
        project_role=ProjectRole.ADMIN,
    )
    # An org action on a named project the caller can see: the org role decides.
    authorize(
        _access(Role.ADMIN),
        Action.ORG_REMOVE_PROJECT,
        _PROJECT,
        project_role=ProjectRole.ADMIN,
    )
    with pytest.raises(ApiError) as exc:
        authorize(
            _access(Role.MEMBER),
            Action.ORG_REMOVE_PROJECT,
            _PROJECT,
            project_role=ProjectRole.ADMIN,
        )
    assert exc.value.status == 403


@pytest.mark.parametrize(
    "action", [Action.PROJECT_READ, Action.ORG_READ, Action.ORG_REMOVE_PROJECT]
)
def test_authorize_without_access_is_404_for_every_action(action):
    with pytest.raises(ApiError) as exc:
        authorize(_access(Role.OWNER), action, _PROJECT, project_role=None)
    assert exc.value.status == 404
    assert exc.value.error == "not found"


def test_authorize_needs_a_computed_project_role():
    with pytest.raises(ValueError):
        authorize(_access(Role.OWNER), Action.PROJECT_READ, _PROJECT)
    with pytest.raises(ValueError):
        authorize(_access(Role.OWNER), Action.ORG_REMOVE_PROJECT, _PROJECT)
    # A project action with no project at all.
    with pytest.raises(ValueError):
        authorize(_access(Role.OWNER), Action.PROJECT_READ)
    with pytest.raises(ValueError):
        authorize(
            _access(Role.OWNER), Action.PROJECT_READ, project_role=ProjectRole.ADMIN
        )


def test_authorize_org_mismatch_is_404():
    other = SimpleNamespace(org_id=2)
    with pytest.raises(ApiError) as exc:
        authorize(
            _access(Role.OWNER),
            Action.PROJECT_READ,
            other,
            project_role=ProjectRole.ADMIN,
        )
    assert exc.value.status == 404
    assert exc.value.error == "not found"


@pytest.mark.parametrize("action", sorted(PROJECT_ACTIONS))
def test_org_access_refuses_a_project_action_at_build_time(action):
    with pytest.raises(ValueError, match="project action"):
        org_access(action)
    org_access(Action.ORG_READ)  # an org action builds
