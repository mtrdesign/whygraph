"""Hosts and tenant isolation over real sessions (M2c plan section 5.4).

Every test here drives a production portal with **no** injected identity, so
``SessionIdentity`` resolves each request from a real session cookie (set by
the GitHub sign-in callback, or ``POST /api/auth/login`` for the password
admin) and :func:`whygraph.portal.hosts.classify` resolves
the organization from the ``Host`` header. The :func:`prod_world` fixture
(plan section 5.1) builds Ada (instance admin, no memberships), Ann (owner of
``quokka``), Bob (owner of ``narwhal`` and member of ``quokka``), and one
project ``api`` per org - inserted directly with ``initialized_at`` set and
its database created directly, because production makes projects only by a
GitHub App import (``test_portal_github_import.py``), which this world has
no app for.

The route sweeps are generated from the live app and
:data:`~test_portal_app.ROUTE_ACTIONS`, as M2b's ``test_portal_tenancy.py``
does, so a new route joins them automatically and a new non-org route fails
:func:`test_every_production_only_route_is_classified_by_host` until it is
classified.

Neighbouring modules, deliberately not duplicated here: the guard itself
(``Host`` classification and its ``421``, the own-host ``Origin`` rule,
``Sec-Fetch-Site``, the security headers, HSTS, ``/mcp``'s ``404``, the CORS
preflight, pages and assets making no session lookup) is
``test_portal_production_guard.py``; the identity routes and the admin page
are ``test_portal_identity_routes.py``; startup and the pure modules are
``test_portal_identity.py`` and ``test_portal_hosts.py``.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`, `audit_log`) are imported

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from starlette.middleware.cors import CORSMiddleware

from github_fake import _git as fixture_git
from test_portal_app import (  # noqa: F401 -- fixtures
    LOCAL_ONLY_ROUTES,
    NON_ORG_ROUTES,
    PRODUCTION_ORG_ROUTES,
    PUBLIC_AUTH_ROUTES,
    ROUTE_ACTIONS,
    V1_ROUTES,
    at,
    claim_instance,
    GitServer,
    env,
    github_sign_in,
    log_in,
    manual_ctx,
    portal_client,
    prod_portal,
    production_env,
)
from test_portal_github_import import (  # noqa: F401 -- fixtures
    app_env,
    github_app_key,
    github_git_server,
)
from test_portal_identity_routes import (  # noqa: F401 -- `audit_log` is a fixture
    audit_log,
    create_org,
    events,
)
from test_portal_tenancy import (
    API_ROUTES,
    ROUTE_REQUESTS,
    TERMINAL,
    OrgWorld,
    _fake_scanner,
    _git,
    _marked_repo,
    _offline_llms,
    _ok,
    _org_scoped_routes,
    _seed_codegraph,
    _seed_history,
    _url,
    assert_no_leak,
    newcomer_github_id,
    seed_usage,
    wait_for,
)
from whygraph.core.context import use_project
from whygraph.db import ensure_initialized
from whygraph.db import get_session as project_session
from whygraph.db.models import ChatSession as ChatSessionRow
from whygraph.portal import db as portal_db
from whygraph.portal import runner as runner_mod
from whygraph.portal.authz import (
    PROJECT_ACTIONS,
    PROJECT_ROLE_ACTIONS,
    ROLE_ACTIONS,
    ProjectRole,
    Role,
)
from whygraph.portal.models import (
    Membership,
    Organization,
    Project,
    ProjectGrant,
    ScanRun,
    Secret,
    User,
)
from whygraph.portal.orgs import add_member
from whygraph.portal.secrets import GITHUB_TOKEN, put_secret

NOT_FOUND = {"error": "not found"}
LOGIN_REQUIRED = {"error": "sign-in required", "code": "login_required"}
GITHUB_IDS = {"quokka": (71, 7101), "narwhal": (72, 7201)}
"""Each org's GitHub App installation and the repository id of its ``api``."""
SOURCE_NOT_ALLOWED = {
    "error": "production organizations add projects from GitHub only",
    "code": "source_not_allowed",
}
HOOK_LOCAL_ONLY = {
    "error": "hook scans exist only in local mode",
    "code": "hook_local_only",
}
READER_ONLY_READS = (
    "instance admins can only read an organization they are not a member of"
)

ADA_EMAIL = "ada@example.com"
"""The password admin of :func:`prod_world`."""

GITHUB_LOGINS = ("ann", "bob")
"""The GitHub accounts of :func:`prod_world` (their short name is the login)."""


# ---------------------------------------------------------------------------
# Which host serves which production-only route (plan section 4.7)
# ---------------------------------------------------------------------------

BASE_ONLY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("/api/auth/bootstrap", "GET"),
        ("/api/auth/bootstrap", "POST"),
        ("/api/auth/github/start", "POST"),
        ("/api/auth/github/callback", "POST"),
        ("/api/auth/login", "POST"),
        ("/api/auth/reset", "POST"),
        ("/api/orgs", "POST"),
        ("/api/github/app/callback", "POST"),
        ("/api/admin/settings", "GET"),
        ("/api/admin/orgs", "GET"),
        ("/api/admin/users", "GET"),
        ("/api/admin/users/{uid}", "PATCH"),
        ("/api/admin/users/{uid}/reset-link", "POST"),
        ("/api/admin/audit", "GET"),
        # The consent page, the code exchange and the caller's connected
        # portals (M2e section 4.4)
        ("/api/connect/validate", "POST"),
        ("/api/connect/projects", "GET"),
        ("/api/connect/authorize", "POST"),
        ("/api/connect/token", "POST"),
        ("/api/connect/tokens", "GET"),
        ("/api/connect/tokens/{uid}", "DELETE"),
    }
)
"""Credential, org-creation, admin and consent routes: ``404`` on an org host."""

ANY_HOST_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("/api/auth/logout", "POST"),
        ("/api/account", "GET"),
        ("/api/account", "PATCH"),
        ("/api/account/password", "POST"),
        ("/api/account/orgs", "GET"),
        ("/api/account/usage", "GET"),  # M2f-2 section 4.11
        ("/api/v1/meta", "GET"),  # public: a connected portal's version check
    }
)
"""Logout, the account routes and ``/api/v1/meta``: served on the base host
and on org hosts."""

ORG_HOST_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("/api/org/members", "GET"),
        ("/api/org/members", "POST"),
        ("/api/org/members/{uid}", "PATCH"),
        ("/api/org/members/{uid}", "DELETE"),
        ("/api/org/membership", "DELETE"),
        ("/api/org/invitations", "GET"),
        ("/api/org/invitations/{uid}", "DELETE"),
        ("/api/org", "DELETE"),
        ("/api/org", "PATCH"),
        ("/api/org/transfer", "POST"),
        ("/api/org/audit", "GET"),
        ("/api/org/audit.csv", "GET"),
        ("/api/github/app/authorize", "POST"),
        ("/api/github/installations", "GET"),
        ("/api/github/installations/{installation_id}/repos", "GET"),
        ("/api/projects/{slug}/connections", "GET"),
        ("/api/projects/{slug}/connections/{uid}", "DELETE"),
        ("/api/projects/{slug}/access", "GET"),
        ("/api/projects/{slug}/access", "PATCH"),
        ("/api/projects/{slug}/access/{user_uid}", "PUT"),
        ("/api/projects/{slug}/access/{user_uid}", "DELETE"),
        # Per-member budgets (M2f-2 section 4.11)
        ("/api/budgets/member-default", "PUT"),
        ("/api/budgets/member-default", "DELETE"),
        ("/api/budgets/members/{uid}", "PUT"),
        ("/api/budgets/members/{uid}", "DELETE"),
        # A member's own usage (M2f-2 section 4.11)
        ("/api/usage/me", "GET"),
        ("/api/usage/me/calls", "GET"),
        ("/api/usage/me.csv", "GET"),
    }
)
"""The members routes (M2d-1 section 4.5), the org's deletion (M2d-2 section
4.8), the GitHub App import page (M2d-2 section 4.4) and a project's
connected portals (M2e section 4.4): org-scoped, so served on org hosts only,
and swept with every other org-scoped route below."""

PROD_API_ROUTES = [
    (method, path)
    for method, path in API_ROUTES
    if (path, method) not in LOCAL_ONLY_ROUTES
]
"""The org-scoped routes a **production** portal serves: local mode's own
``/api/platform/*`` are ``404`` there, before ``current_user``, so the session
sweeps below leave them out (``test_portal_link.py`` asserts that ``404``)."""


PRODUCTION_ONLY_ROUTES = (
    PUBLIC_AUTH_ROUTES | NON_ORG_ROUTES | PRODUCTION_ORG_ROUTES | V1_ROUTES
)
"""Every route local mode does not serve: the ones that name no org (public
auth, ``user.self``, ``instance.admin``), the members routes and the
bearer-only ``/api/v1`` routes."""


def test_every_production_only_route_is_classified_by_host() -> None:
    """A new production-only route must be put on a host before the sweeps run.

    :data:`~test_portal_app.V1_ROUTES` is its own class: org host, but a
    bearer token rather than a session, so none of this module's session
    sweeps covers it (``test_portal_connect.py``'s bearer sweep does).
    """
    classes = (BASE_ONLY_ROUTES, ANY_HOST_ROUTES, ORG_HOST_ROUTES, V1_ROUTES)
    assert frozenset().union(*classes) == PRODUCTION_ONLY_ROUTES
    assert sum(len(c) for c in classes) == len(PRODUCTION_ONLY_ROUTES)  # disjoint


# ---------------------------------------------------------------------------
# What production refuses in a route body (plan section 4.11)
# ---------------------------------------------------------------------------

PRODUCTION_REFUSALS: dict[tuple[str, str], tuple[int, dict]] = {
    # The local-folder flow does not exist in production.
    ("GET", "/api/portal/repos"): (404, NOT_FOUND),
    ("POST", "/api/portal/check-path"): (404, NOT_FOUND),
    # The source policy (the sweep's body is a local folder).
    ("POST", "/api/projects"): (403, SOURCE_NOT_ALLOWED),
}
"""Routes whose body refuses in production, after ``authorize()``, and how."""


# ---------------------------------------------------------------------------
# The prod_world fixture (plan section 5.1)
# ---------------------------------------------------------------------------


@dataclass
class ProdWorld:
    """Two production orgs over real sessions, plus the people who reach them.

    Attributes
    ----------
    client : fastapi.testclient.TestClient
        The production portal; one cookie jar, so :meth:`sign_in` swaps who
        the next request is.
    env : types.SimpleNamespace
        The ``production_env`` namespace (its ``shared`` holds the repos).
    quokka, narwhal : OrgWorld
        The two orgs, each holding a project ``api`` whose every visible
        value carries the org's mark (reused from ``test_portal_tenancy``).
    uids, ids : dict of str to str or int
        ``users.uid`` and ``users.id`` by short name.
    scanner : types.SimpleNamespace
        The fake scan child: ``calls()`` lists what really ran.
    """

    client: TestClient
    env: SimpleNamespace
    quokka: OrgWorld
    narwhal: OrgWorld
    uids: dict[str, str]
    ids: dict[str, int]
    scanner: SimpleNamespace

    def other(self, org: OrgWorld) -> OrgWorld:
        """The org that is not ``org`` (``ROUTE_REQUESTS`` calls this)."""
        return self.narwhal if org is self.quokka else self.quokka

    def owner_of(self, org: OrgWorld) -> str:
        """The short name of ``org``'s owner."""
        return "ann" if org is self.quokka else "bob"

    def sign_in(self, name: str) -> httpx.Response:
        """Sign ``name`` in for real: GitHub for Ann and Bob, a password for Ada."""
        if name in GITHUB_LOGINS:
            response = github_sign_in(self.client, name)
        else:
            assert name == "ada", name
            response = log_in(self.client, ADA_EMAIL)
        assert response.status_code == 200, (name, response.text)
        return response

    def sign_out(self) -> None:
        """Drop the session cookie (the row stays; nothing is revoked)."""
        self.client.cookies.clear()


def _insert_project(
    org_id: int,
    slug: str,
    name: str,
    root,
    created_by: int,
    *,
    github: tuple[int, int] | None = None,
    remote_url: str | None = None,
    default_branch: str | None = None,
) -> int:
    """Insert an initialized project row directly and return its id.

    Production makes projects only by a GitHub App import; the row and the
    project database are made the way the import would have. With its
    GitHub identity (``github`` is the installation and repository id) a
    scan syncs against the fake's git server; without it the row serves
    every route but a scan. Its root stays absolute (no clone is made).
    """
    with use_project(manual_ctx(root, slug=slug)):
        ensure_initialized()
    with portal_db.get_session() as session:
        project = Project(
            org_id=org_id,
            slug=slug,
            name=name,
            source="github",
            root=str(root),
            github_installation_id=github[0] if github else None,
            github_repo_id=github[1] if github else None,
            remote_url=remote_url,
            default_branch=default_branch,
            initialized_at="2026-10-03T00:00:00+00:00",
            created_by=created_by,
        )
        session.add(project)
        session.flush()
        return project.id


def _publish(server: GitServer, org: OrgWorld) -> tuple[str, str]:
    """Serve ``org``'s marked repo as ``<org>/api`` under its own installation.

    Returns the clone URL and the default branch. The scan's sync fetches
    from here and checks the same commits out again.
    """
    installation, repo_id = GITHUB_IDS[org.slug]
    full_name = f"{org.slug}/api"
    branch = _git(org.root, "symbolic-ref", "--short", "HEAD").strip()
    path = server.repos_dir / f"{full_name}.git"
    path.parent.mkdir(parents=True, exist_ok=True)
    fixture_git("clone", "-q", "--bare", str(org.root), str(path))
    fixture_git("--git-dir", str(path), "update-server-info")
    server.fake.add_installation(installation, org.slug, account_type="Organization")
    server.fake.add_repo(
        repo_id, full_name, installation, default_branch=branch, path=path
    )
    url = server.clone_url(full_name)
    _git(org.root, "remote", "add", "origin", url)  # as the import's clone has
    return url, branch


def _add_prod_project(
    world: ProdWorld, server: GitServer, org: OrgWorld, defaults: dict
) -> None:
    """Give ``org`` its marked ``api``: repo, graph, history, config and a scan."""
    client = world.client
    org.root = _marked_repo(world.env, org.mark)
    _seed_codegraph(org.root, org.mark)
    url, branch = _publish(server, org)
    owner = world.owner_of(org)
    org.project_id = _insert_project(
        org.org_id,
        "api",
        org.name,
        org.root,
        world.ids[owner],
        github=GITHUB_IDS[org.slug],
        remote_url=url,
        default_branch=branch,
    )
    world.sign_in(owner)
    prefix = at(org.slug)
    _ok(client.put(prefix + "/api/portal/defaults", json=defaults))
    # A project key beside the org keys, and a forge, as M2b's fixture does.
    _ok(
        client.put(
            prefix + "/api/projects/api/config",
            json={
                "config": {"scan": {"forge": "auto"}},
                "secrets": {"llm": {"openai": org.secrets["project_openai"]}},
            },
        )
    )
    _seed_history(org.root)
    org.marker_sha = _git(org.root, "rev-parse", "HEAD").strip()
    org.first_sha = _git(org.root, "rev-list", "--max-parents=0", "HEAD").strip()
    org.run_id = _ok(client.post(prefix + "/api/projects/api/scans"), 202)["run_id"]
    created = client.post(
        prefix + "/api/projects/api/chat/sessions", json={"title": org.session_title}
    )
    org.session_id = _ok(created, 201)["id"]


def _wait_idle(world: ProdWorld, org: OrgWorld) -> list[dict]:
    """Wait until every run of ``org``'s ``api`` has ended; return them."""
    world.sign_in(world.owner_of(org))
    url = at(org.slug) + "/api/projects/api/scans"
    return wait_for(
        lambda: (
            (runs := _ok(world.client.get(url))["runs"])
            and all(r["status"] in TERMINAL for r in runs)
            and runs
        )
    )


@pytest.fixture
def prod_world(
    production_env: SimpleNamespace,
    app_env: GitServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ProdWorld]:
    """Plan section 5.1's ``prod_world``: Ada, Ann, Bob and two ``api``s.

    Every account and org is made through the real routes (bootstrap, GitHub
    sign-in through the fake GitHub, create org, add member), so the
    sessions the sweeps use are the ones a browser would hold. Ada is the
    password admin; Ann and Bob are GitHub accounts. Bob's extra ``quokka``
    membership is made by Ann through ``POST /api/org/members``. The portal
    has the GitHub App (``app_env``), and each ``api`` is served by the
    fake's git server, so its scans sync the way an imported project's do.
    """
    env = production_env
    github_fake = app_env.fake
    scanner = _fake_scanner(env, monkeypatch)
    _offline_llms(monkeypatch)
    for login in GITHUB_LOGINS:
        github_fake.add_user(login)
    for mark in ("quokka", "narwhal"):  # the sweep's `_newcomer` accounts
        login = f"{mark}-newcomer"
        github_fake.add_user(login, id=newcomer_github_id(login))
    with prod_portal() as client:
        claim_instance(client, ADA_EMAIL, display_name="Ada")
        client.cookies.clear()
        quokka = OrgWorld("quokka", "quokka", {})
        narwhal = OrgWorld("narwhal", "narwhal", {})
        for org, owner in ((quokka, "ann"), (narwhal, "bob")):
            assert github_sign_in(client, owner).status_code == 200
            assert create_org(client, org.slug, org.slug.title()).status_code == 201
        client.cookies.clear()
        uids, ids = {}, {}
        with portal_db.get_session() as session:
            people = [("ada", User.email == ADA_EMAIL)] + [
                (login, User.github_login == login) for login in GITHUB_LOGINS
            ]
            for name, where in people:
                user = session.exec(select(User).where(where)).one()
                uids[name], ids[name] = user.uid, user.id
            for org in (quokka, narwhal):
                org.org_id = session.exec(
                    select(Organization.id).where(Organization.slug == org.slug)
                ).one()
        quokka.owner_uid, narwhal.owner_uid = uids["ann"], uids["bob"]
        # Bob is also a plain member of quokka (the role matrix over real
        # sessions, and the membership the removal test drops), added by
        # quokka's owner the way the Members page does.
        assert github_sign_in(client, "ann").status_code == 200
        added = client.post(
            at("quokka") + "/api/org/members",
            json={"github_login": "bob", "role": "member"},
        )
        assert added.status_code == 201, added.text
        client.cookies.clear()
        world = ProdWorld(client, env, quokka, narwhal, uids, ids, scanner)
        # narwhal first, so its secret rows are the older ones: a scope
        # filter that forgot the org would let quokka's rows win.
        narwhal.secrets = {
            "anthropic": "sk-narwhal-N7N7",
            "project_openai": "sk-narwhal-proj-N6N6",
        }
        _add_prod_project(
            world,
            app_env,
            narwhal,
            {
                "config": {"llm": {"model": narwhal.model}},
                "secrets": {
                    "llm": {"anthropic": narwhal.secrets["anthropic"]},
                },
            },
        )
        quokka.secrets = {
            "anthropic": "sk-quokka-Q7Q7",
            "openai": "sk-quokka-Q8Q8",
            "project_openai": "sk-quokka-proj-Q6Q6",
        }
        _add_prod_project(
            world,
            app_env,
            quokka,
            {
                "config": {"llm": {"model": quokka.model}},
                "secrets": {
                    "llm": {
                        tag: quokka.secrets[tag] for tag in ("anthropic", "openai")
                    },
                },
            },
        )
        for org in (narwhal, quokka):
            (first,) = _wait_idle(world, org)
            assert first["status"] == "ok", first
            seed_usage(client, org)
        world.sign_out()
        yield world
        scanner.hold.unlink(missing_ok=True)


def test_the_fixture_is_two_marked_orgs_over_real_sessions(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    state = w.client.app.state.portal
    assert state.mode == "production" and state.builtin_org_slug is None
    # Production has no shared folders and runs no port reconcile.
    assert state.shared_folders == () and state.port_change is None
    assert w.quokka.project_id != w.narwhal.project_id
    assert w.quokka.marker_sha != w.narwhal.marker_sha
    for org in (w.quokka, w.narwhal):
        w.sign_in(w.owner_of(org))
        body = _ok(w.client.get(at(org.slug) + "/api/projects/api"))
        assert (body["name"], body["root"]) == (org.name, str(org.root))
        # M2d-2's summary fields carry the org's mark, so the sweeps'
        # assert_no_leak covers them on every route that shows them.
        assert body["github_full_name"].startswith(f"{org.slug}/api")
        assert body["installation_account"] == org.slug
        assert body["access_lost"] is False
        assert_no_leak(str(body), w.other(org), where=org.slug)
    # Ada holds a session but no membership anywhere.
    with portal_db.get_session() as session:
        assert (
            session.exec(
                select(Membership).where(Membership.user_id == w.ids["ada"])
            ).all()
            == []
        )


# ---------------------------------------------------------------------------
# Every org-scoped route, over real sessions and real hosts
# ---------------------------------------------------------------------------


def _call(
    world: ProdWorld, org: OrgWorld, method: str, path: str, spec
) -> httpx.Response:  # noqa: ANN001, E501
    """Call ``method path`` on ``org``'s host, the way ``ROUTE_REQUESTS`` says."""
    return world.client.request(
        method,
        at(org.slug) + _url(path, org),
        params=spec.query(org) if spec.query else None,
        json=spec.body(world, org) if spec.body else None,
    )


def test_the_sweeps_cover_every_live_route(prod_world: ProdWorld) -> None:
    """The collected routes are the running production app's, with requests."""
    assert _org_scoped_routes(prod_world.client.app) == API_ROUTES
    assert len(API_ROUTES) > 30
    assert set(ROUTE_REQUESTS) == set(API_ROUTES)
    assert set(PRODUCTION_REFUSALS) <= set(API_ROUTES)
    # Every route production serves is swept; only the local-only ones are out.
    assert {(p, m) for (m, p) in set(API_ROUTES) - set(PROD_API_ROUTES)} == (
        LOCAL_ONLY_ROUTES
    )


@pytest.mark.parametrize(
    ("method", "path"),
    PROD_API_ROUTES,
    ids=[f"{m} {p}" for m, p in PROD_API_ROUTES],
)
def test_every_org_scoped_route_is_isolated_over_real_sessions(
    prod_world: ProdWorld, method: str, path: str
) -> None:
    """Each owner's session reaches only her own ``api``, on her own host.

    Ann's session on ``narwhal`` (she is no member) and on a host naming no
    org answer one indistinguishable ``404``; on her own host each route
    answers with ``quokka``'s project and none of ``narwhal``'s markers -
    and the same for Bob.
    """
    w = prod_world
    spec = ROUTE_REQUESTS.get((method, path))
    assert spec is not None, f"add a ROUTE_REQUESTS entry for {method} {path}"
    w.sign_in("ann")
    for prefix in (at("narwhal"), at("nosuchorg")):
        response = w.client.request(method, prefix + _url(path, w.narwhal))
        assert response.status_code == 404, (prefix, method, path, response.text)
        assert response.json() == NOT_FOUND, (prefix, method, path)
    for org in (w.narwhal, w.quokka):
        other = w.other(org)
        w.sign_in(w.owner_of(org))
        response = _call(w, org, method, path, spec)
        where = f"{org.slug}: {method} {path}"
        if (method, path) in PRODUCTION_REFUSALS:
            status, body = PRODUCTION_REFUSALS[(method, path)]
            assert response.status_code == status, (where, response.text)
            assert response.json() == body, where
            continue
        assert response.status_code == spec.status, (where, response.text)
        assert_no_leak(response.text, other, where=where)
        for expected in spec.shows(org):
            assert expected.lower() in response.text.lower(), (where, expected)
        if response.status_code == 404:  # a domain 404, not the binding's
            assert response.json() != NOT_FOUND, where
        if spec.check is not None:
            spec.check(response.json() if response.content else None, w, org)


def test_every_org_route_is_404_on_the_base_host(prod_world: ProdWorld) -> None:
    """The base host names no org, so every org-scoped route is a ``404``."""
    w = prod_world
    w.sign_in("ann")
    for method, path in API_ROUTES:
        response = w.client.request(method, at() + _url(path, w.quokka))
        assert response.status_code == 404, (method, path, response.text)
        assert response.json() == NOT_FOUND, (method, path)


def _fill_uid(path: str, uid: str) -> str:
    return path.replace("{uid}", uid)


def test_every_base_only_route_is_404_on_an_org_host(prod_world: ProdWorld) -> None:
    """Credentials, org creation and the admin page live on the base host."""
    w = prod_world
    w.sign_in("ada")  # an instance admin, so a 404 can only be the host gate
    for path, method in sorted(BASE_ONLY_ROUTES):
        url = at("quokka") + _fill_uid(path, w.uids["ann"])
        response = w.client.request(method, url, json={})
        assert response.status_code == 404, (method, path, response.text)
        assert response.json() == NOT_FOUND, (method, path)


def test_the_account_routes_answer_on_both_kinds_of_host(
    prod_world: ProdWorld,
) -> None:
    """``ANY_HOST_ROUTES`` are never refused for naming the wrong host."""
    w = prod_world
    for prefix in (at(), at("quokka"), at("narwhal")):
        w.sign_in("ann")
        for path, method in sorted(ANY_HOST_ROUTES):
            body = {"display_name": "Ann"} if method == "PATCH" else {}
            response = w.client.request(method, prefix + path, json=body)
            assert response.status_code != 404, (prefix, method, path, response.text)
            assert response.json() != NOT_FOUND, (prefix, method, path)


# ---------------------------------------------------------------------------
# No cookie
# ---------------------------------------------------------------------------


def test_without_a_session_every_non_public_route_needs_one_on_its_own_host(
    prod_world: ProdWorld,
) -> None:
    """``401 login_required`` where the route exists; ``404`` on the wrong host.

    The host gate of a ``user.self`` / ``instance.admin`` route is declared
    before ``current_user`` (plan section 4.7), so on the wrong host it
    stays a ``404``. An org-scoped route has no such gate: ``current_user``
    runs before the org lookup, so on the base host it answers ``401`` too -
    the ``404`` of :func:`test_every_org_route_is_404_on_the_base_host`
    needs a session to reach the org check.
    """
    w = prod_world
    w.sign_out()
    for method, path in PROD_API_ROUTES:
        for prefix in (at("quokka"), at()):
            response = w.client.request(method, prefix + _url(path, w.quokka))
            assert response.status_code == 401, (prefix, method, path, response.text)
            assert response.json() == LOGIN_REQUIRED, (prefix, method, path)
    for path, method in sorted(NON_ORG_ROUTES):
        here = at() if (path, method) in BASE_ONLY_ROUTES else at("quokka")
        url = _fill_uid(path, w.uids["ann"])
        response = w.client.request(method, here + url, json={})
        assert response.status_code == 401, (method, path, response.text)
        assert response.json() == LOGIN_REQUIRED, (method, path)
        if (path, method) in BASE_ONLY_ROUTES:  # the wrong host stays a 404
            wrong = w.client.request(method, at("quokka") + url, json={})
            assert wrong.status_code == 404, (method, path, wrong.text)
            assert wrong.json() == NOT_FOUND, (method, path)


# ---------------------------------------------------------------------------
# Local-only features in production (plan section 4.11)
# ---------------------------------------------------------------------------


def test_a_member_is_refused_before_the_production_refusals(
    prod_world: ProdWorld,
) -> None:
    """The refusals sit after ``authorize()``, so a member gets ``403 forbidden``."""
    w = prod_world
    w.sign_in("bob")  # a plain member of quokka
    seen = set()
    for method, path in sorted(PRODUCTION_REFUSALS):
        action = ROUTE_ACTIONS[(path, method)]
        spec = ROUTE_REQUESTS[(method, path)]
        response = w.client.request(
            method,
            at("quokka") + _url(path, w.quokka),
            json=spec.body(w, w.quokka) if spec.body else None,
        )
        if action in {str(a) for a in ROLE_ACTIONS[Role.MEMBER]}:
            # org.read: a member passes authorize and meets the refusal.
            status, body = PRODUCTION_REFUSALS[(method, path)]
            assert response.status_code == status, (method, path, response.text)
            assert response.json() == body, (method, path)
        else:
            assert response.status_code == 403, (method, path, response.text)
            assert response.json() == {
                "error": f"your role (member) cannot {action}",
                "code": "forbidden",
                "action": action,
            }, (method, path)
            seen.add((method, path))
    assert seen == set(PRODUCTION_REFUSALS) - {("GET", "/api/portal/repos")}


def test_the_source_policy_in_production(prod_world: ProdWorld) -> None:
    """GitHub only (API included); an import first needs the user's GitHub authorization."""
    w = prod_world
    w.sign_in("ann")
    prefix = at("quokka")
    local = w.client.post(
        prefix + "/api/projects", json={"source": "local", "path": str(w.quokka.root)}
    )
    assert (local.status_code, local.json()) == (403, SOURCE_NOT_ALLOWED)
    github = w.client.post(
        prefix + "/api/projects",
        json={"source": "github", "installation_id": 7, "repo_id": 501},
    )
    assert (github.status_code, github.json()["code"]) == (
        401,
        "github_authorization_required",
    ), github.text
    for body in (
        {"source": "github", "installation_id": 7, "repo_id": 501, "path": "/x"},
        {"source": "github", "url": "https://github.com/acme/api"},
        {"source": "local", "path": "/x", "repo_id": 501},
    ):
        response = w.client.post(prefix + "/api/projects", json=body)
        assert response.status_code == 422, (body, response.text)
    for method, path in (("GET", "repos"), ("POST", "check-path")):
        response = w.client.request(
            method, f"{prefix}/api/portal/{path}", json={"path": "/x"}
        )
        assert (response.status_code, response.json()) == (404, NOT_FOUND), path
    listed = _ok(w.client.get(prefix + "/api/projects"))["projects"]
    assert [(p["source"], p["source_supported"]) for p in listed] == [("github", True)]


def test_a_github_token_is_never_stored_or_injected_in_production(
    prod_world: ProdWorld,
) -> None:
    """No personal access token in production (M2d-2 section 0.2 #13)."""
    w = prod_world
    w.sign_in("ann")
    prefix = at("quokka")
    token = "ghp_prod_refused_T9T9"
    for url in ("/api/portal/defaults", "/api/projects/api/config"):
        response = w.client.put(prefix + url, json={"secrets": {"github_token": token}})
        assert response.status_code == 422, (url, response.text)
        assert response.json()["code"] == "not_in_production", url
        assert token not in response.text
        # Deleting one stays allowed.
        _ok(w.client.put(prefix + url, json={"secrets": {"github_token": None}}))
    with portal_db.get_session() as session:
        assert (
            session.exec(select(Secret.id).where(Secret.kind == GITHUB_TOKEN)).all()
            == []
        )
        # One stored before this release (org and project scope) is never
        # injected into a production child.
        for project_id in (None, w.quokka.project_id):
            put_secret(
                session,
                kind=GITHUB_TOKEN,
                value=token,
                project_id=project_id,
                org_id=w.quokka.org_id,
            )
    w.client.app.state.portal.contexts.invalidate(None)
    before = len(w.scanner.calls())
    _ok(w.client.post(prefix + "/api/projects/api/scans"), 202)
    _wait_idle(w, w.quokka)
    (call,) = w.scanner.calls()[before:]
    assert "GH_TOKEN" not in call["env"]
    assert token not in json.dumps(call)


def test_production_has_no_setup_route_and_no_mcp_url(prod_world: ProdWorld) -> None:
    w = prod_world
    w.sign_in("ann")
    setup = w.client.post(at() + "/api/portal/setup", json={"display_name": "Eve"})
    assert setup.status_code == 404 and setup.json() == NOT_FOUND
    body = _ok(w.client.get(at("quokka") + "/api/projects/api"))
    assert body["mcp_url"] is None


def test_a_hook_scan_is_refused_and_starts_nothing(prod_world: ProdWorld) -> None:
    """Hook scans exist only in local mode; a manual one still runs.

    Moved here from ``test_portal_tenancy.py`` with M2c step 5 (plan
    section 7): the hook refusal is a production-mode behaviour, and
    ``prod_world`` already holds a directly inserted project.
    """
    w = prod_world
    w.sign_in("ann")
    before = len(w.scanner.calls())
    url = at("quokka") + "/api/projects/api/scans"
    hook = w.client.post(url, json={"trigger": "hook"})
    assert hook.status_code == 403
    assert hook.json() == HOOK_LOCAL_ONLY
    _ok(w.client.post(url, json={"trigger": "manual"}), 202)
    _wait_idle(w, w.quokka)
    assert len(w.scanner.calls()) == before + 1  # only the manual scan ran


def test_a_hooks_config_change_is_refused_and_never_synced(
    prod_world: ProdWorld,
) -> None:
    """``[scan].hooks`` is not writable in production (M2d-2 section 0.2 #21)."""
    w = prod_world
    w.sign_in("ann")
    url = at("quokka") + "/api/projects/api/config"
    response = w.client.put(url, json={"config": {"scan": {"hooks": False}}})
    assert response.status_code == 422, response.text
    assert response.json()["keys"] == ["scan.hooks"]
    assert "hooks" not in _ok(w.client.get(url))["config"].get("scan", {})
    hooks_dir = w.quokka.root / ".git" / "hooks"
    assert not [p for p in hooks_dir.glob("post-*") if p.suffix != ".sample"]


def test_the_catch_up_never_runs_in_production(
    prod_world: ProdWorld,
) -> None:
    """It is local-mode only, so a moved HEAD queues nothing."""
    w = prod_world
    before = len(w.scanner.calls())
    with portal_db.get_session() as session:
        for org in (w.quokka, w.narwhal):
            row = session.get(Project, org.project_id)
            row.last_scanned_head = org.first_sha  # HEAD moved since
            session.add(row)
    runner = w.client.app.state.portal.runner
    for start in (runner.catch_up,):
        w.client.portal.call(start)
    with portal_db.get_session() as session:
        queued = session.exec(
            select(ScanRun.id).where(col(ScanRun.trigger) == "hook")
        ).all()
    assert queued == []
    assert len(w.scanner.calls()) == before


# ---------------------------------------------------------------------------
# The instance admin as `reader` (plan section 4.9)
# ---------------------------------------------------------------------------

READER_ACTIONS = {str(action) for action in ROLE_ACTIONS[Role.READER]}
"""``org.read`` and ``org.usage``: the org actions a ``reader`` may do."""

READER_PROJECT_ACTIONS = {
    str(action) for action in PROJECT_ROLE_ACTIONS[ProjectRole.VIEWER]
}
"""``project.read``: a ``reader`` is a viewer on every project (M2f-1 plan
section 0.2 #19)."""

READ_ROUTES = sorted(
    (m, p)
    for (m, p) in PROD_API_ROUTES
    if m == "GET" and ROUTE_ACTIONS[(p, m)] in READER_ACTIONS | READER_PROJECT_ACTIONS
)
OTHER_ROUTES = sorted(set(PROD_API_ROUTES) - set(READ_ROUTES))


def _is_binding_404(response: httpx.Response) -> bool:
    """Whether this is the org binding's ``404`` (never a domain or SSE one)."""
    if response.status_code != 404:
        return False
    try:
        return response.json() == NOT_FOUND
    except ValueError:  # a streamed SSE body is not JSON
        return False


def test_the_reader_route_split_is_the_planned_one() -> None:
    assert READER_ACTIONS == {"org.read", "org.usage"}
    assert READER_PROJECT_ACTIONS == {"project.read"}
    # A reader lists an org's members, and changes nothing about them.
    assert ("GET", "/api/org/members") in READ_ROUTES
    assert {
        ("POST", "/api/org/members"),
        ("PATCH", "/api/org/members/{uid}"),
        ("DELETE", "/api/org/members/{uid}"),
        ("DELETE", "/api/org/membership"),
        # Invitations are the members admins' (M2f-1 section 4.8), even to read.
        ("GET", "/api/org/invitations"),
        ("DELETE", "/api/org/invitations/{uid}"),
    } <= set(OTHER_ROUTES)
    assert ("POST", "/api/projects/{slug}/node/rationale") in OTHER_ROUTES
    assert ("GET", "/api/projects/{slug}/node/rationale") in READ_ROUTES
    # A reader reads a project's Overview (M2f-3 section 4.10) as a viewer.
    assert ("GET", "/api/projects/{slug}/overview") in READ_ROUTES
    # A reader reads the org's usage and budgets (M2f-2 section 9.2 D2), not
    # a project's usage (a project admin's) and changes no budget or price.
    assert {
        ("GET", "/api/usage"),
        ("GET", "/api/usage/calls"),
        ("GET", "/api/usage.csv"),
        ("GET", "/api/usage/me"),
        ("GET", "/api/budgets"),
        ("GET", "/api/prices"),
    } <= set(READ_ROUTES)
    assert {
        ("GET", "/api/projects/{slug}/usage"),
        ("PUT", "/api/prices"),
        ("DELETE", "/api/prices"),
        ("PUT", "/api/budgets/org"),
    } <= set(OTHER_ROUTES)
    assert len(READ_ROUTES) > 10 and len(OTHER_ROUTES) > 10


def test_an_instance_admin_reads_every_org_and_writes_to_none(
    prod_world: ProdWorld,
) -> None:
    """Ada reads both orgs as ``reader``; every other action is ``403``."""
    w = prod_world
    w.sign_in("ada")
    for org in (w.quokka, w.narwhal):
        prefix = at(org.slug)
        state = _ok(w.client.get(prefix + "/api/portal/state"))
        assert state["org"] == {
            "slug": org.slug,
            "name": org.slug.title(),
            "role": "reader",
            "default_project_role": "contributor",
        }
        for method, path in READ_ROUTES:
            response = w.client.request(method, prefix + _url(path, org))
            where = f"{org.slug}: {method} {path}"
            if (method, path) in PRODUCTION_REFUSALS:
                status, body = PRODUCTION_REFUSALS[(method, path)]
                assert response.status_code == status, (where, response.text)
                assert response.json() == body, where
                continue
            # Neither the role refusal nor current_org's 404: a data 404
            # (an unscanned PR / issue) is fine, the binding's is not.
            assert response.status_code != 403, (where, response.text)
            assert not _is_binding_404(response), where
        for method, path in OTHER_ROUTES:
            spec = ROUTE_REQUESTS[(method, path)]
            response = w.client.request(
                method,
                prefix + _url(path, org),
                params=spec.query(org) if spec.query else None,
                json=spec.body(w, org) if spec.body else None,
            )
            where = f"{org.slug}: {method} {path}"
            assert response.status_code == 403, (where, response.text)
            body = response.json()
            assert body["code"] == "forbidden", where
            if method == "GET":  # a GET whose action a reader lacks
                action = ROUTE_ACTIONS[(path, method)]
                if action in {str(a) for a in PROJECT_ACTIONS}:
                    expected = f"your role on this project (viewer) cannot {action}"
                else:
                    expected = f"your role (reader) cannot {action}"
                assert body["error"] == expected, where
            else:  # current_org's GET / HEAD rule, before authorize
                assert body["error"] == READER_ONLY_READS, where


def test_a_real_membership_wins_over_the_reader_role(prod_world: ProdWorld) -> None:
    """Bob promoted to instance admin stays a member of quokka and owner of narwhal."""
    w = prod_world
    w.sign_in("ada")
    promoted = w.client.patch(
        at() + f"/api/admin/users/{w.uids['bob']}", json={"is_instance_admin": True}
    )
    assert promoted.status_code == 200, promoted.text
    w.sign_in("bob")
    roles = {
        org.slug: _ok(w.client.get(at(org.slug) + "/api/portal/state"))["org"]["role"]
        for org in (w.quokka, w.narwhal)
    }
    assert roles == {"quokka": "member", "narwhal": "owner"}
    # A member may chat, which a reader may not.
    created = w.client.post(
        at("quokka") + "/api/projects/api/chat/sessions", json={"title": "as a member"}
    )
    assert created.status_code == 201, created.text


def test_demoting_an_instance_admin_closes_the_org_on_the_next_request(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    w.sign_in("ada")
    assert w.client.get(at("quokka") + "/api/projects/api").status_code == 200
    with portal_db.get_session() as session:
        session.get(User, w.ids["ada"]).is_instance_admin = False
    refused = w.client.get(at("quokka") + "/api/projects/api")
    assert refused.status_code == 404 and refused.json() == NOT_FOUND


def test_each_reader_request_writes_one_security_event(
    prod_world: ProdWorld, audit_log: pytest.LogCaptureFixture
) -> None:
    w = prod_world
    w.sign_in("ada")
    audit_log.clear()
    paths = ["/api/projects", "/api/projects/api", "/api/projects/api/tree"]
    for path in paths:
        assert w.client.get(at("quokka") + path).status_code == 200, path
    records = [r for r in events(audit_log) if r["event"] == "reader_request"]
    assert len(records) == len(paths)
    for record, path in zip(records, paths, strict=True):
        assert record["uid"] == w.uids["ada"]
        assert record["org"] == "quokka"
        assert (record["method"], record["path"]) == ("GET", path)
    # A member's request writes none.
    w.sign_in("ann")
    audit_log.clear()
    assert w.client.get(at("quokka") + "/api/projects").status_code == 200
    assert [r for r in events(audit_log) if r["event"] == "reader_request"] == []


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------


def test_removing_a_membership_applies_on_the_next_request(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    w.sign_in("bob")
    assert w.client.get(at("quokka") + "/api/projects").status_code == 200
    with portal_db.get_session() as session:
        membership = session.get(Membership, (w.quokka.org_id, w.ids["bob"]))
        session.delete(membership)
    refused = w.client.get(at("quokka") + "/api/projects")
    assert refused.status_code == 404 and refused.json() == NOT_FOUND
    # His own org is untouched, on the same session.
    assert w.client.get(at("narwhal") + "/api/projects").status_code == 200


def test_an_orgs_member_list_is_invisible_on_another_orgs_host(
    prod_world: ProdWorld,
) -> None:
    w = prod_world
    w.sign_in("ann")  # owner of quokka, no member of narwhal
    refused = w.client.get(at("narwhal") + "/api/org/members")
    assert refused.status_code == 404 and refused.json() == NOT_FOUND
    own = _ok(w.client.get(at("quokka") + "/api/org/members"))
    assert sorted(m["github_login"] for m in own) == ["ann", "bob"]
    w.sign_in("bob")  # in both orgs: each host lists only its own
    narwhal = _ok(w.client.get(at("narwhal") + "/api/org/members"))
    assert [(m["github_login"], m["role"]) for m in narwhal] == [("bob", "owner")]


def test_another_orgs_member_cannot_be_changed_through_this_orgs_host(
    prod_world: ProdWorld,
) -> None:
    """A ``uid`` from narwhal is ``not_member`` on quokka's host, whatever its role."""
    w = prod_world
    assert github_sign_in(w.client, "cy").status_code == 200
    w.sign_in("bob")
    added = w.client.post(
        at("narwhal") + "/api/org/members",
        json={"github_login": "cy", "role": "admin"},
    )
    cy = _ok(added, 201)["uid"]
    w.sign_in("ann")  # owner of quokka
    for method, body in (("PATCH", {"role": "member"}), ("DELETE", None)):
        here = w.client.request(
            method, at("quokka") + f"/api/org/members/{cy}", json=body
        )
        assert here.status_code == 404, (method, here.text)
        assert here.json()["code"] == "not_member", method
        there = w.client.request(
            method, at("narwhal") + f"/api/org/members/{cy}", json=body
        )
        assert there.status_code == 404 and there.json() == NOT_FOUND, method
    w.sign_in("bob")
    narwhal = _ok(w.client.get(at("narwhal") + "/api/org/members"))
    assert ("cy", "admin") in [(m["github_login"], m["role"]) for m in narwhal]


def test_memberships_cannot_store_the_reader_role(prod_world: ProdWorld) -> None:
    """``reader`` is internal: neither ``add_member`` nor the database takes it."""
    w = prod_world
    with pytest.raises(ValueError, match="cannot be stored"):
        with portal_db.get_session() as session:
            add_member(
                session, org_id=w.narwhal.org_id, user_id=w.ids["ada"], role="reader"
            )
    with pytest.raises(IntegrityError, match="ck_memberships_role"):
        with portal_db.get_engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO memberships (org_id, user_id, role, created_at, "
                    "welcome_pending) VALUES (:org, :user, 'reader', "
                    "'2026-10-03T00:00:00+00:00', false)"
                ),
                {"org": w.narwhal.org_id, "user": w.ids["ada"]},
            )


# ---------------------------------------------------------------------------
# Per-user chat (M2d-1 plan section 4.7)
# ---------------------------------------------------------------------------


def _chat_row_owner(org: OrgWorld, session_id: int) -> str | None:
    """The ``owner_uid`` stored on one of ``org``'s ``api`` chat sessions."""
    with use_project(manual_ctx(org.root, slug="api")), project_session() as session:
        return session.get(ChatSessionRow, session_id).owner_uid


def test_each_member_sees_and_touches_only_their_own_chat_sessions(
    prod_world: ProdWorld,
) -> None:
    """Ann (owner) and Bob (member) of quokka: the other's session is a plain 404."""
    w = prod_world
    chat = at("quokka") + "/api/projects/api/chat/sessions"
    w.sign_in("bob")
    bobs = _ok(w.client.post(chat, json={"title": "bob's chat"}), 201)["id"]
    assert _chat_row_owner(w.quokka, bobs) == w.uids["bob"]
    assert _chat_row_owner(w.quokka, w.quokka.session_id) == w.uids["ann"]
    # A row with no owner (pre-M2d-1) belongs to nobody in production.
    with use_project(manual_ctx(w.quokka.root, slug="api")), project_session() as s:
        ownerless = ChatSessionRow(
            title="ownerless",
            provider="openai",
            model="gpt-4o",
            created_at="2026-10-01T00:00:00+00:00",
            updated_at="2026-10-01T00:00:00+00:00",
        )
        s.add(ownerless)
        s.commit()
        s.refresh(ownerless)
        ownerless_id = ownerless.id

    seen = {}
    for name, own, others in (
        ("bob", bobs, (w.quokka.session_id, ownerless_id)),
        ("ann", w.quokka.session_id, (bobs, ownerless_id)),
    ):
        w.sign_in(name)
        seen[name] = [row["id"] for row in _ok(w.client.get(chat))]
        assert _ok(w.client.get(f"{chat}/{own}"))["id"] == own
        for other in others:
            for method, suffix, body in (
                ("GET", "", None),
                ("PATCH", "", {"title": "taken over"}),
                ("DELETE", "", None),
                ("POST", "/messages", {"content": "hello"}),
            ):
                refused = w.client.request(method, f"{chat}/{other}{suffix}", json=body)
                where = (name, method, suffix, other)
                assert refused.status_code == 404, (where, refused.text)
                assert refused.json() == {
                    "error": f"session {other} not found",
                    "code": "not_found",
                }
    assert seen == {"bob": [bobs], "ann": [w.quokka.session_id]}
    # Nothing the refused calls aimed at changed.
    anns = _ok(w.client.get(f"{chat}/{w.quokka.session_id}"))
    assert (anns["title"], anns["messages"]) == (w.quokka.session_title, [])
    w.sign_in("bob")
    mine = _ok(w.client.get(f"{chat}/{bobs}"))
    assert (mine["title"], mine["messages"]) == ("bob's chat", [])


def test_a_reader_has_no_chat(prod_world: ProdWorld) -> None:
    """Ada reads quokka as ``reader``, which carries no ``project.chat``."""
    w = prod_world
    w.sign_in("ada")
    chat = at("quokka") + "/api/projects/api/chat/sessions"
    for method, url, body in (
        ("GET", chat, None),
        ("POST", chat, {"title": "a reader's"}),
        ("GET", f"{chat}/{w.quokka.session_id}", None),
        ("POST", f"{chat}/{w.quokka.session_id}/messages", {"content": "hi"}),
    ):
        refused = w.client.request(method, url, json=body)
        assert refused.status_code == 403, (method, url, refused.text)
        assert refused.json()["code"] == "forbidden", (method, url)
    # A reader is a viewer on every project (M2f-1 plan section 0.2 #19).
    assert w.client.get(chat).json() == {
        "error": "your role on this project (viewer) cannot project.chat",
        "code": "forbidden",
        "action": "project.chat",
    }


# ---------------------------------------------------------------------------
# An open scan-event stream ends when its access goes (plan section 4.8)
# ---------------------------------------------------------------------------


def _session_token(client: TestClient) -> str:
    token = client.cookies.get("whygraph_session")
    assert token, "not signed in"
    return token


def _set_project_access(
    w: ProdWorld, *, restricted: bool | None = None, grant: str | None = None
) -> None:
    """Restrict quokka's ``api`` and / or grant Bob a role on it (straight in the DB)."""
    with portal_db.get_session() as session:
        if restricted is not None:
            session.get(Project, w.quokka.project_id).restricted = restricted
        if grant is not None:
            session.add(
                ProjectGrant(
                    org_id=w.quokka.org_id,
                    project_id=w.quokka.project_id,
                    user_id=w.ids["bob"],
                    role=grant,
                )
            )


def _drop_bobs_grant(w: ProdWorld) -> None:
    with portal_db.get_session() as session:
        session.delete(session.get(ProjectGrant, (w.quokka.project_id, w.ids["bob"])))


def _set_default_none_and_bob_admin(w: ProdWorld) -> None:
    with portal_db.get_session() as session:
        session.get(Organization, w.quokka.org_id).default_project_role = "none"
        membership = session.exec(
            select(Membership).where(
                Membership.org_id == w.quokka.org_id,
                Membership.user_id == w.ids["bob"],
            )
        ).one()
        membership.role = "admin"


@pytest.mark.parametrize(
    "revoke",
    [
        "removed",
        "disabled",
        "signed_out",
        # M2f-1 plan section 6.3 #5: project access, not the membership, goes.
        "grant_removed",
        "restricted",
        "demoted_under_none",
    ],
)
def test_an_open_scan_event_stream_ends_when_its_access_goes(
    prod_world: ProdWorld, monkeypatch: pytest.MonkeyPatch, revoke: str
) -> None:
    """Bob follows a held quokka run; losing access in any way cuts it."""
    w = prod_world
    monkeypatch.setattr(runner_mod, "ACCESS_CHECK_SEC", 0.0)
    if revoke == "grant_removed":
        _set_project_access(w, restricted=True, grant="viewer")
    elif revoke == "demoted_under_none":
        _set_default_none_and_bob_admin(w)
    w.scanner.hold.touch()
    w.sign_in("ann")
    run_id = _ok(w.client.post(at("quokka") + "/api/projects/api/scans"), 202)["run_id"]
    runs_url = at("quokka") + "/api/projects/api/scans"
    wait_for(
        lambda: (
            next(r for r in _ok(w.client.get(runs_url))["runs"] if r["id"] == run_id)[
                "status"
            ]
            == "running"
        )
    )
    # Each actor keeps their own session: the client is cleared between
    # sign-ins, since a sign-in over a session revokes it.
    actor = {
        "removed": "ann",
        "disabled": "ada",
        "signed_out": "bob",
        "demoted_under_none": "ann",
    }.get(revoke, "ann")
    tokens = {"ann": _session_token(w.client)}
    w.client.cookies.clear()
    w.sign_in("ada")
    tokens["ada"] = _session_token(w.client)
    w.client.cookies.clear()
    w.sign_in("bob")
    tokens["bob"] = _session_token(w.client)

    runner = w.client.app.state.portal.runner
    real_events = runner.events
    opened, done = threading.Event(), threading.Event()
    revoked: list[httpx.Response] = []

    async def events(*args, **kwargs):
        response = await real_events(*args, **kwargs)
        opened.set()
        return response

    monkeypatch.setattr(runner, "events", events)

    def take_access_away() -> None:
        assert opened.wait(20)
        if revoke in ("grant_removed", "restricted"):
            if revoke == "grant_removed":
                _drop_bobs_grant(w)
            else:
                _set_project_access(w, restricted=True)
            revoked.append(httpx.Response(204))
            if not done.wait(20):
                w.scanner.hold.unlink(missing_ok=True)
            return
        method, url, body = {
            "removed": (
                "DELETE",
                at("quokka") + f"/api/org/members/{w.uids['bob']}",
                None,
            ),
            "disabled": (
                "PATCH",
                at() + f"/api/admin/users/{w.uids['bob']}",
                {"disabled": True},
            ),
            "signed_out": ("POST", at() + "/api/auth/logout", None),
            "demoted_under_none": (
                "PATCH",
                at("quokka") + f"/api/org/members/{w.uids['bob']}",
                {"role": "member"},
            ),
        }[revoke]
        revoked.append(
            w.client.request(
                method,
                url,
                json=body,
                headers={"cookie": f"whygraph_session={tokens[actor]}"},
            )
        )
        if not done.wait(20):  # never hang: let the run end on its own
            w.scanner.hold.unlink(missing_ok=True)

    thread = threading.Thread(target=take_access_away)
    thread.start()
    try:
        stream = w.client.get(
            at("quokka") + f"/api/projects/api/scans/{run_id}/events",
            headers={"cookie": f"whygraph_session={tokens['bob']}"},
        )
    finally:
        done.set()
        thread.join()
    assert len(revoked) == 1 and revoked[0].status_code in (200, 204), revoked
    assert stream.status_code == 200, stream.text
    blocks = [b for b in stream.text.split("\n\n") if b.strip()]
    assert "event: end" in blocks[-1], blocks[-1]
    end = json.loads(blocks[-1].split("data: ", 1)[1])
    assert end == {
        "type": "end",
        "run_id": run_id,
        "status": None,
        "summary": None,
        "reason": "access_revoked",
    }
    # The run itself goes on: only the stream was cut.
    w.client.cookies.clear()
    owner = w.client.get(
        runs_url, headers={"cookie": f"whygraph_session={tokens['ann']}"}
    )
    assert next(r for r in _ok(owner)["runs"] if r["id"] == run_id)["status"] == (
        "running"
    )
    if revoke == "removed":  # Bob's session itself still works elsewhere
        own = w.client.get(
            at("narwhal") + "/api/projects",
            headers={"cookie": f"whygraph_session={tokens['bob']}"},
        )
        assert own.status_code == 200, own.text


# ---------------------------------------------------------------------------
# No CORS, in either mode
# ---------------------------------------------------------------------------


def test_local_mode_grants_no_cors_preflight_either(env: SimpleNamespace) -> None:
    """The custom-header defence rests on no preflight ever succeeding."""
    with portal_client() as client:
        assert not any(m.cls is CORSMiddleware for m in client.app.user_middleware)
        response = client.options(
            "/api/projects",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-whygraph-client",
            },
        )
        assert response.status_code == 403
        assert not any(h.startswith("access-control-allow") for h in response.headers)
