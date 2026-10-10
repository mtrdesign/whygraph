"""Tenancy isolation and the role matrix over HTTP (M2b plan sections 5.1-5.4).

The ``two_orgs`` fixture builds two organizations that each hold a project
``api``: the built-in ``local`` (owner alice, member carol, admin dave) and
``beta`` (owner bob), plus erin, who belongs to no org. Every org-visible
value is marked - ``local``'s repo, names, commits, symbols, model and chat
session carry ``quokka``, ``beta``'s carry ``narwhal``, and each secret ends
in a distinctive four-character hint - so a response that leaks the other
org's data is caught by a check on its body, not only by its status code.

The route sweeps are parametrized from the live app's routes
(``iter_route_contexts``): a new route is swept automatically, and fails
until :data:`ROUTE_REQUESTS` says how to call it.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`) imported from test_portal_app

from __future__ import annotations

import atexit
import dataclasses
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import httpx
import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlmodel import select

from conftest import HeaderIdentity, build_fake_codegraph_db
from test_portal_app import (  # noqa: F401 -- `env`, `production_env` are fixtures
    _NODES,
    LOCAL_ONLY_ROUTES,
    NON_ORG_ACTIONS,
    NON_ORG_ROUTES,
    PORT,
    PRODUCTION_ORG_ROUTES,
    PUBLIC_API_ROUTES,
    ROUTE_ACTIONS,
    V1_ROUTES,
    _git,
    at,
    env,
    manual_ctx,
    portal_client,
    prod_portal,
    production_env,
)
from test_portal_mcp import MCP_HEADERS, _rpc, _sse_json
from whygraph.core.context import use_project
from whygraph.db import ensure_initialized
from whygraph.db import get_session as project_session
from whygraph.db.models import Commit, CommitFileChange
from whygraph.mcp import rationale as mcp_rationale
from whygraph.mcp.targets import repo_root
from whygraph.portal import db as portal_db
from whygraph.portal import runner as runner_mod
from whygraph.portal.app import create_portal_app
from whygraph.portal.authz import PROJECT_ACTIONS, PROJECT_ROLE_ACTIONS, ProjectRole
from whygraph.portal.authz import ROLE_ACTIONS as ROLE_TABLE
from whygraph.portal.authz import Role
from whygraph.portal.models import (
    Membership,
    Organization,
    PriceOverride,
    Project,
    ProjectGrant,
    ScanRun,
    Setting,
    UsageEvent,
    User,
)
from whygraph.portal.orgs import add_member, create_org
from whygraph.portal.usage_store import reload_org_prices
from whygraph.serve import chat as serve_chat
from whygraph.services.llm import LlmError
from whygraph.services.llm.chat import TextDelta, TurnDone

FAKE_SCAN = Path(__file__).parent / "fixtures" / "fake_scan.py"
TERMINAL = ("ok", "failed", "interrupted", "cancelled")
NOT_FOUND = {"error": "not found"}
LOGIN_REQUIRED = {"error": "sign-in required", "code": "login_required"}


# ---------------------------------------------------------------------------
# The two_orgs fixture (plan section 5.1)
# ---------------------------------------------------------------------------


@dataclass
class OrgWorld:
    """One org's ``api`` project and the markers a leak of it would show."""

    slug: str
    mark: str  # "quokka" / "narwhal": inside every org-visible value
    owner: dict[str, str]  # the org owner's request headers
    org_id: int = 0
    root: Path = Path()
    project_id: int = 0
    first_sha: str = ""
    marker_sha: str = ""
    run_id: int = 0
    session_id: int = 0
    secrets: dict[str, str] = field(default_factory=dict)  # label -> value
    owner_uid: str = ""  # the owner's users.uid (production's members routes)

    @property
    def name(self) -> str:
        return f"{self.mark.title()} API"

    @property
    def model(self) -> str:
        return f"anthropic/{self.mark}-model"

    @property
    def qualified_name(self) -> str:
        return f"{self.mark}.{self.mark}_fn"

    @property
    def session_title(self) -> str:
        return f"{self.mark} chat"

    def hint(self, label: str) -> str:
        """The last four characters, all ``secret_status`` ever shows."""
        return self.secrets[label][-4:]

    def needles(self) -> list[str]:
        """Lower-case substrings only this org's data contains."""
        needles = [self.mark, self.marker_sha, self.marker_sha[:10]]
        needles += [value[-4:].lower() for value in self.secrets.values()]
        return [needle for needle in needles if needle]  # unset markers are ""


@dataclass
class World:
    client: TestClient
    env: SimpleNamespace
    local: OrgWorld
    beta: OrgWorld
    users: dict[str, dict[str, str]]  # name -> {"x-test-user": uid}
    scanner: SimpleNamespace

    def as_(self, user: str, org: str) -> dict[str, str]:
        return {**self.users[user], "x-test-org": org}

    def other(self, org: OrgWorld) -> OrgWorld:
        return self.beta if org is self.local else self.local


def _fake_scanner(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch):
    """Make ``tests/fixtures/fake_scan.py`` the scan child: never a real scan."""
    record, hold = env.tmp / "scans.jsonl", env.tmp / "hold"
    argv = [
        sys.executable,
        str(FAKE_SCAN),
        "--record",
        str(record),
        "--hold",
        str(hold),
    ]
    monkeypatch.setenv("WHYGRAPH_SCAN_CMD", shlex.join(argv))

    def calls() -> list[dict]:
        if not record.is_file():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]

    return SimpleNamespace(record=record, hold=hold, calls=calls)


class _OfflineChat:
    """``make_chat_client`` stand-in: no provider is ever reached."""

    def list_models(self) -> list:
        raise LlmError("offline in tests")


class _StubRationale:
    """``RationaleGenerator`` stand-in: fails with where it ran, no LLM call."""

    @classmethod
    def from_config(cls, config):  # noqa: ANN001, ANN206
        return cls()

    def generate(self, evidence, **kwargs):  # noqa: ANN001, ANN003, ANN201
        shas = sorted(e.commit.sha for e in evidence)
        raise LlmError(f"stub generator ran in {repo_root()} over {shas}")


def _offline_llms(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_turn(*, client, history, registry=None, **kwargs):  # noqa: ANN001, ANN003, ANN202
        yield TextDelta(text=f"answer from {repo_root()}")
        yield TurnDone("stop", 1, 2)

    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: _OfflineChat())
    monkeypatch.setattr(serve_chat, "run_turn", fake_run_turn)
    monkeypatch.setattr(mcp_rationale, "RationaleGenerator", _StubRationale)


_GIT_ID = ("-c", "user.email=tester@example.com", "-c", "user.name=Test User")


_TEMPLATES: dict[str, Path] = {}
"""Built repos by mark, copied per test: the sweep builds two orgs per case."""


def _marked_repo(env: SimpleNamespace, mark: str, name: str = "api") -> Path:
    """``<shared>/<mark>/<name>``: two ``sample.py`` commits, then a ``<mark>.py`` one."""
    if mark not in _TEMPLATES:
        parent = Path(tempfile.mkdtemp(prefix="whygraph-tenancy-"))
        atexit.register(shutil.rmtree, parent, True)
        _TEMPLATES[mark] = _build_marked_repo(parent / "repo", mark)
    root = env.shared / mark / name
    shutil.copytree(_TEMPLATES[mark], root, symlinks=True)
    return Path(os.path.realpath(root))


def _build_marked_repo(root: Path, mark: str) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "sample.py").write_text("line one\nline two\n")
    _git(root, "add", "sample.py")
    _git(root, *_GIT_ID, "commit", "-q", "-m", "first commit")
    (root / "sample.py").write_text("line one\nline two\nline three\n")
    _git(root, *_GIT_ID, "commit", "-q", "-am", "second commit")
    (root / f"{mark}.py").write_text(f"def {mark}_fn():\n    return '{mark}'\n")
    _git(root, "add", f"{mark}.py")
    _git(root, *_GIT_ID, "commit", "-q", "-m", f"{mark} marker commit")
    return root


def _seed_codegraph(root: Path, mark: str) -> None:
    """The shared ``sample.py`` symbols plus ``<mark>.py`` / ``<mark>.<mark>_fn``."""
    marked = {
        "language": "python",
        "start_line": 1,
        "end_line": 2,
        "docstring": None,
        "file_path": f"{mark}.py",
    }
    nodes = [
        *_NODES,
        {
            **marked,
            "id": f"n_{mark}_file",
            "kind": "file",
            "name": f"{mark}.py",
            "qualified_name": f"{mark}.py",
            "signature": None,
        },
        {
            **marked,
            "id": f"n_{mark}_fn",
            "kind": "function",
            "name": f"{mark}_fn",
            "qualified_name": f"{mark}.{mark}_fn",
            "signature": f"def {mark}_fn()",
        },
    ]
    edges = [
        ("n_file", "n_fn", "contains"),
        (f"n_{mark}_file", f"n_{mark}_fn", "contains"),
    ]
    (root / ".codegraph").mkdir(exist_ok=True)
    build_fake_codegraph_db(
        root / ".codegraph" / "codegraph.db", nodes=nodes, edges=edges
    )


def _seed_history(root: Path) -> None:
    """The repo's commits and file changes as scanned, described rows."""
    log = _git(
        root, "log", "--name-status", "--format=%x1e%H%x1f%P%x1f%aI%x1f%cI%x1f%s"
    )
    with use_project(manual_ctx(root, slug="api")), project_session() as session:
        for record in filter(None, log.split("\x1e")):
            header, *rows = record.strip("\n").split("\n")
            sha, parents, authored, committed, subject = header.split("\x1f")
            changes = [row.split("\t") for row in rows if row]
            session.add(
                Commit(
                    sha=sha,
                    parent_shas=parents,
                    author_name="Test User",
                    author_email="tester@example.com",
                    authored_at=authored,
                    committed_at=committed,
                    subject=subject,
                    body="",
                    files_changed=len(changes),
                    insertions=1,
                    deletions=0,
                    scanned_at="2026-05-01T00:00:00+00:00",
                    llm_description=f"Mechanical summary of {subject}.",
                )
            )
            session.flush()
            for change_type, path in changes:
                session.add(
                    CommitFileChange(
                        commit_sha=sha,
                        path=path,
                        change_type=change_type,
                        lines_added=1,
                    )
                )
        session.commit()


def _ok(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def wait_for(predicate: Callable[[], Any], timeout: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.03)


def runs(client: TestClient, headers: dict, slug: str = "api") -> list[dict]:
    return _ok(client.get(f"/api/projects/{slug}/scans", headers=headers))["runs"]


def wait_idle(client: TestClient, headers: dict, slug: str = "api") -> list[dict]:
    return wait_for(
        lambda: (
            (rs := runs(client, headers, slug))
            and all(r["status"] in TERMINAL for r in rs)
            and rs
        )
    )


def _add_org_project(world: World, org: OrgWorld, defaults: dict) -> None:
    """Add, name, configure, initialize (which starts its scan) and seed ``org``'s ``api``."""
    client, owner = world.client, org.owner
    org.root = _marked_repo(world.env, org.mark)
    _seed_codegraph(org.root, org.mark)
    added = client.post(
        "/api/projects", json={"source": "local", "path": str(org.root)}, headers=owner
    )
    assert _ok(added, 201)["project"]["slug"] == "api"
    _ok(client.patch("/api/projects/api", json={"name": org.name}, headers=owner))
    _ok(client.put("/api/portal/defaults", json=defaults, headers=owner))
    # A project key beside the org keys, and a forge, so a scan child gets
    # the GitHub token (section 5.4).
    config = {
        "config": {"scan": {"forge": "auto"}},
        "secrets": {"llm": {"openai": org.secrets["project_openai"]}},
    }
    _ok(client.put("/api/projects/api/config", json=config, headers=owner))
    init = client.post("/api/projects/api/init", json={"agents": []}, headers=owner)
    assert _ok(init)["marker_written"] is True
    # The first Initialize queues the project's first scan (M2f-3 R2).
    org.run_id = init.json()["initial_run_id"]
    assert isinstance(org.run_id, int)
    _seed_history(org.root)
    org.marker_sha = _git(org.root, "rev-parse", "HEAD").strip()
    org.first_sha = _git(org.root, "rev-list", "--max-parents=0", "HEAD").strip()
    session = client.post(
        "/api/projects/api/chat/sessions",
        json={"title": org.session_title},
        headers=owner,
    )
    org.session_id = _ok(session, 201)["id"]
    with portal_db.get_session() as db:
        org.project_id = db.exec(
            select(Project.id).where(Project.root == str(org.root))
        ).one()


def seed_usage(client: TestClient, org: OrgWorld) -> None:
    """Marked ledger rows and a price override for ``org`` (M2f-2 section 6.3 #3).

    One chat call by the project's creator (the owner) and one scan call by
    the System actor on ``org``'s ``api``, both on ``org.model`` with a
    marked subject, plus an org price override for ``org.model`` - so a
    usage, CSV or price answer that leaks the other org shows its mark.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with portal_db.get_session() as session:
        owner = session.get(User, session.get(Project, org.project_id).created_by)
        common = {
            "org_id": org.org_id,
            "project_id": org.project_id,
            "project_slug": "api",
            "project_name": org.name,
            "provider": "anthropic",
            "model_requested": org.model,
            "key_scope": "org",
            "cost_source": "estimated",
            "input_tokens": 1000,
            "output_tokens": 100,
            "created_at": now,
        }
        session.add(
            UsageEvent(
                **common,
                actor_kind="member",
                user_id=owner.id,
                actor_label=owner.display_name,
                source="chat",
                task="chat",
                chat_session_id=org.session_id,
                subject=f"{org.mark}.py",
                cost_usd=Decimal("0.25"),
            )
        )
        session.add(
            UsageEvent(
                **common,
                actor_kind="system",
                actor_label="System",
                source="scan",
                task="analyze",
                scan_run_id=org.run_id,
                subject=org.marker_sha,
                cost_usd=Decimal("0.5"),
            )
        )
        session.add(
            PriceOverride(
                org_id=org.org_id,
                provider="anthropic",
                model=org.model,
                input_per_mtok=Decimal("1"),
                output_per_mtok=Decimal("2"),
                updated_at=now,
            )
        )
    reload_org_prices(client.app.state.portal.prices, org.org_id)


def _seed_orgs(client: TestClient) -> tuple[OrgWorld, OrgWorld, dict]:
    """Setup (alice owns ``local``), ``beta`` owned by bob, carol, dave and erin."""
    setup = client.post("/api/portal/setup", json={"display_name": "Alice"})
    users = {"alice": {"x-test-user": _ok(setup, 201)["user"]["uid"]}}
    with portal_db.get_session() as session:
        local_id = session.get(Setting, 1).builtin_org_id
        beta_id = create_org(session, slug="bravo", name="Beta").id
        made = {
            n: User(display_name=n.title()) for n in ("bob", "carol", "dave", "erin")
        }
        session.add_all(made.values())
        session.flush()
        add_member(session, org_id=beta_id, user_id=made["bob"].id, role="owner")
        add_member(session, org_id=local_id, user_id=made["carol"].id, role="member")
        add_member(session, org_id=local_id, user_id=made["dave"].id, role="admin")
        users |= {n: {"x-test-user": u.uid} for n, u in made.items()}
    local = OrgWorld("local", "quokka", {**users["alice"], "x-test-org": "local"})
    beta = OrgWorld("bravo", "narwhal", {**users["bob"], "x-test-org": "bravo"})
    local.org_id, beta.org_id = local_id, beta_id
    return local, beta, users


@pytest.fixture
def two_orgs(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Two orgs that each hold ``api`` (plan section 5.1; see the module docstring)."""
    scanner = _fake_scanner(env, monkeypatch)
    _offline_llms(monkeypatch)
    with portal_client(identity=HeaderIdentity()) as client:
        local, beta, users = _seed_orgs(client)
        world = World(client, env, local, beta, users, scanner)
        # beta first: its secret rows are the older ones, so a scope filter
        # that forgot the org would let local's rows win (section 5.4).
        beta.secrets = {
            "anthropic": "sk-beta-narwhal-N7N7",
            "github": "ghp_beta_narwhal_N9N9",
            "project_openai": "sk-beta-narwhal-proj-N6N6",
        }
        _add_org_project(
            world,
            beta,
            {
                "config": {"llm": {"model": beta.model}},
                "secrets": {
                    "llm": {"anthropic": beta.secrets["anthropic"]},
                    "github_token": beta.secrets["github"],
                },
            },
        )
        local.secrets = {
            "anthropic": "sk-local-quokka-Q7Q7",
            "openai": "sk-local-quokka-Q8Q8",
            "deepseek": "sk-local-quokka-Q5Q5",  # a provider beta has no key for
            "github": "ghp_local_quokka_Q9Q9",
            "project_openai": "sk-local-quokka-proj-Q6Q6",
        }
        _add_org_project(
            world,
            local,
            {
                "config": {"llm": {"model": local.model}},
                "secrets": {
                    "llm": {
                        tag: local.secrets[tag]
                        for tag in ("anthropic", "openai", "deepseek")
                    },
                    "github_token": local.secrets["github"],
                },
            },
        )
        for org in (beta, local):
            (first,) = wait_idle(client, org.owner)
            assert first["status"] == "ok", first
            seed_usage(client, org)
        yield world
        scanner.hold.unlink(missing_ok=True)


def assert_no_leak(
    text: str, other: OrgWorld, *, where: str, skip: tuple[str, ...] = ()
) -> None:
    """``text`` contains none of ``other``'s markers (bar ``skip``)."""
    lowered = text.lower()
    for needle in other.needles():
        if needle not in skip:
            assert needle not in lowered, f"{where} leaks {needle!r}: {text[:800]}"


def test_the_two_orgs_are_marked_apart(two_orgs: World) -> None:
    w = two_orgs
    assert w.local.project_id != w.beta.project_id
    assert w.local.marker_sha != w.beta.marker_sha
    assert w.local.run_id != w.beta.run_id
    for org in (w.local, w.beta):
        body = _ok(w.client.get("/api/projects/api", headers=org.owner))
        assert (body["name"], body["root"]) == (org.name, str(org.root))
        assert_no_leak(json.dumps(body), w.other(org), where=org.slug)


def test_the_same_slug_binds_each_orgs_own_project(env: SimpleNamespace) -> None:
    """Item 2 before any slug is bound: two ``api``s added, then read and renamed.

    ``two_orgs`` itself configures each ``api`` through its slug, so a
    lookup that ignored the org breaks that fixture first; this test needs
    nothing but the two adds.
    """
    with portal_client(identity=HeaderIdentity()) as client:
        local, beta, _ = _seed_orgs(client)
        for org in (beta, local):
            org.root = _marked_repo(env, org.mark)
            added = client.post(
                "/api/projects",
                json={"source": "local", "path": str(org.root)},
                headers=org.owner,
            )
            assert _ok(added, 201)["project"]["slug"] == "api"
        for org in (beta, local):
            renamed = client.patch(
                "/api/projects/api", json={"name": org.name}, headers=org.owner
            )
            assert _ok(renamed)["root"] == str(org.root)
        for org, other in ((beta, local), (local, beta)):
            for path in ("/api/projects/api", "/api/projects/api/config"):
                response = client.get(path, headers=org.owner)
                assert response.status_code == 200, (org.slug, response.text)
                assert_no_leak(response.text, other, where=f"{org.slug}: {path}")
            body = _ok(client.get("/api/projects/api", headers=org.owner))
            assert (body["name"], body["root"]) == (org.name, str(org.root))


# ---------------------------------------------------------------------------
# The route sweep (plan section 5.2 items 1, 2, 4 and 7)
# ---------------------------------------------------------------------------


def _org_scoped_routes(app) -> list[tuple[str, str]]:  # noqa: ANN001
    """``(method, path)`` of every org-scoped ``/api`` route a **session** reaches.

    The public routes, the ones that name no org (``user.self`` /
    ``instance.admin``, M2c plan section 4.7) and the bearer-only
    :data:`~test_portal_app.V1_ROUTES` (a connection token, never a
    session; their sweep is ``test_portal_connect.py``'s) are left out. The
    production-only members routes are in: the production sweeps of
    ``test_portal_hosts_isolation.py`` cover them, and the local ones skip
    them (:data:`LOCAL_API_ROUTES`).
    """
    found = set()
    for rc in iter_route_contexts(app.routes):
        path = rc.path or ""
        if not path.startswith("/api") or getattr(rc, "dependant", None) is None:
            continue
        if path not in PUBLIC_API_ROUTES:
            found |= {
                (method, path)
                for method in rc.methods
                if method != "HEAD"
                and (path, method) not in NON_ORG_ROUTES
                and (path, method) not in V1_ROUTES
            }
    return sorted(found, key=lambda route: (route[1], route[0]))


def _collect_routes() -> list[tuple[str, str]]:
    """The live app's routes, at collection time (no lifespan, no DB)."""
    with tempfile.TemporaryDirectory() as data, pytest.MonkeyPatch.context() as mp:
        mp.setenv(portal_db.DATA_ENV_VAR, data)
        return _org_scoped_routes(create_portal_app(port=PORT, instance_lock=False))


API_ROUTES = _collect_routes()

LOCAL_API_ROUTES = [
    (method, path)
    for method, path in API_ROUTES
    if (path, method) not in PRODUCTION_ORG_ROUTES
]
"""The org-scoped routes local mode serves: the members routes are ``404``
there by design (M2d-1 plan section 0.2 #19)."""

PATH_PARAMS: dict[str, Callable[[OrgWorld], str]] = {
    "{slug}": lambda o: "api",
    "{run_id}": lambda o: str(o.run_id),
    "{sha}": lambda o: o.marker_sha,
    "{number}": lambda o: "1",
    "{session_id}": lambda o: str(o.session_id),
    "{uid}": lambda o: o.owner_uid,
    "{user_uid}": lambda o: o.owner_uid,  # an owner: 409 org_admin
    "{installation_id}": lambda o: "7",
    "{link_id}": lambda o: "nolink",  # no pending link: 410 link_expired
    "{provider}": lambda o: "openrouter",  # no key stored: 409 key_missing
}
"""How to fill each path parameter for an org; an unmapped one fails the sweep."""


def _url(path: str, org: OrgWorld) -> str:
    for placeholder, fill in PATH_PARAMS.items():
        path = path.replace(placeholder, fill(org))
    assert "{" not in path, f"no PATH_PARAMS entry for a parameter of {path}"
    return path


@dataclass(frozen=True)
class Call:
    """How to call a route in its own org, and what its answer must show."""

    status: int
    query: Callable[[OrgWorld], dict] | None = None
    body: Callable[[World, OrgWorld], Any] | None = None
    shows: Callable[[OrgWorld], list[str]] = lambda o: []
    check: Callable[[Any, World, OrgWorld], None] | None = None
    # The body lists every repo of the (instance-wide) shared folders, so
    # the other org's root and name may appear - never as registered.
    discovery: bool = False


def _repos_check(body: dict, w: World, o: OrgWorld) -> None:
    registered = {r["path"]: r["registered"] for r in body["repos"]}
    assert registered[str(o.root)] is True
    assert registered[str(w.other(o).root)] is False  # item 4


def _projects_check(body: dict, w: World, o: OrgWorld) -> None:
    assert [(p["slug"], p["name"]) for p in body["projects"]] == [("api", o.name)]


def _new_run_check(body: dict, w: World, o: OrgWorld) -> None:
    with portal_db.get_session() as session:
        assert session.get(ScanRun, body["run_id"]).project_id == o.project_id


def _runs_check(body: dict, w: World, o: OrgWorld) -> None:
    assert [r["id"] for r in body["runs"]] == [o.run_id]


def _deleted_check(body: dict, w: World, o: OrgWorld) -> None:
    with portal_db.get_session() as session:
        assert session.get(Project, o.project_id) is None


def _qn(o: OrgWorld) -> dict:
    return {"qualified_name": o.qualified_name}


def newcomer_github_id(login: str) -> int:
    """The GitHub id of a ``<mark>-newcomer`` (the fake GitHub must know it too)."""
    return sum(map(ord, login)) + 900_000


def _newcomer(o: OrgWorld) -> str:
    """A GitHub account in no org, inserted directly once; its login.

    Adding resolves the login on GitHub and matches the id (M2f-1), so
    ``prod_world`` registers the same account with the fake GitHub.
    """
    login = f"{o.mark}-newcomer"
    with portal_db.get_session() as session:
        exists = session.exec(select(User.id).where(User.github_login == login))
        if exists.first() is None:
            session.add(
                User(
                    display_name=login,
                    github_id=newcomer_github_id(login),
                    github_login=login,
                )
            )
    return login


def _invitations_check(body: list, w: World, o: OrgWorld) -> None:
    assert isinstance(body, list)
    assert all("email" not in i for i in body)


def _members_check(body: list, w: World, o: OrgWorld) -> None:
    owners = [m["uid"] for m in body if m["role"] == "owner"]
    assert owners == [o.owner_uid]
    assert all("email" not in m for m in body)


def _added_check(body: dict, w: World, o: OrgWorld) -> None:
    assert (body["github_login"], body["role"]) == (f"{o.mark}-newcomer", "member")
    with portal_db.get_session() as session:
        user_id = session.exec(select(User.id).where(User.uid == body["uid"])).one()
        assert session.get(Membership, (o.org_id, user_id)).role == "member"


def _no_connections(body: list, w: World, o: OrgWorld) -> None:
    assert body == []


def _access_check(body: dict, w: World, o: OrgWorld) -> None:
    assert body["restricted"] is False
    assert o.owner_uid in [p["uid"] for p in body["people"]]


def _org_settings_check(body: dict, o: OrgWorld) -> None:
    assert (body["slug"], body["default_project_role"]) == (o.slug, "contributor")


def _audit_check(body: dict, o: OrgWorld) -> None:
    assert all(event["org"] == o.slug for event in body["events"])


def _owner_kept_check(body: dict, w: World, o: OrgWorld) -> None:
    assert (body["uid"], body["role"]) == (o.owner_uid, "owner")


def _budgets_check(body: dict, w: World, o: OrgWorld) -> None:
    assert set(body) == {
        "month",
        "resets_at",
        "org",
        "member_default",
        "members",
        "projects",
        "alerts",
        "unpriced_calls",
    }
    assert all(p["slug"] == "api" for p in body["projects"])


def _usage_check(body: dict, w: World, o: OrgWorld) -> None:
    assert set(body) == {"range", "totals", "split", "series", "groups"}
    assert [(g["key"], g["label"]) for g in body["groups"]] == [("api", o.name)]


def _project_usage_check(body: dict, w: World, o: OrgWorld) -> None:
    assert set(body) == {"range", "totals", "split", "series", "members"}
    assert body["totals"]["calls"] == 2
    assert body["split"]["scans"]["calls"] == 1


def _onboarding_check(body: dict) -> None:
    assert [i["id"] for i in body["items"]][-1] == "agent"
    assert all(set(i) == {"id", "done", "can_act"} for i in body["items"])


def _overview_check(body: dict, w: World, o: OrgWorld) -> None:
    assert set(body) == {"coverage", "events", "last_failure", "usage", "agents"}
    assert {e["run_id"] for e in body["events"]} <= {o.run_id}
    assert sum(t["calls"] for t in body["usage"]["by_task"]) == 2
    # No agent calls and no tokens in either world: no person or machine
    # (the lists are production's, null locally).
    assert body["agents"]["people"] in (None, [])
    assert body["agents"]["connections"] in (None, [])


def _my_usage_check(body: dict, w: World, o: OrgWorld) -> None:
    # The owner's own row only: never the System's scan call.
    assert body["totals"]["calls"] == 1
    assert body["split"]["scans"]["calls"] == 0


def _prices_check(body: dict, w: World, o: OrgWorld) -> None:
    overrides = [r for r in body["rows"] if r["origin"] == "override"]
    assert [(r["provider"], r["model"]) for r in overrides] == [("anthropic", o.model)]


def _price_body(w: World, o: OrgWorld) -> dict:
    return {
        "provider": "anthropic",
        "model": o.model,
        "input_per_mtok": 3,
        "output_per_mtok": 15,
    }


def _budget_body(amount: int) -> Callable[[World, OrgWorld], dict]:
    return lambda w, o: {"monthly_usd": amount, "hard_stop": False}


ROUTE_REQUESTS: dict[tuple[str, str], Call] = {
    # Portal level
    ("GET", "/api/portal/repos"): Call(200, check=_repos_check, discovery=True),
    ("POST", "/api/portal/check-path"): Call(
        200, body=lambda w, o: {"path": str(o.root)}, shows=lambda o: [str(o.root)]
    ),
    ("GET", "/api/portal/defaults"): Call(
        200, shows=lambda o: [o.model, o.hint("anthropic")]
    ),
    ("PUT", "/api/portal/defaults"): Call(
        200,
        body=lambda w, o: {"config": {"llm": {"model": o.model}}},
        shows=lambda o: [o.model, o.hint("anthropic")],
    ),
    # Key tests (M2f-3 section 4.13): openrouter has no key in any world, so
    # the sweep never reaches a provider (test_portal_keys.py tests for real)
    ("POST", "/api/portal/defaults/keys/{provider}/test"): Call(
        409, shows=lambda o: ["key_missing"]
    ),
    ("POST", "/api/projects/{slug}/keys/{provider}/test"): Call(
        409, shows=lambda o: ["key_missing"]
    ),
    # (local-only; the marked repos have no GitHub origin)
    ("POST", "/api/projects/{slug}/github-token/test"): Call(
        422, shows=lambda o: ["not_github"]
    ),
    ("GET", "/api/projects"): Call(200, check=_projects_check),
    # Production's members page (swept on org hosts only, over prod_world)
    ("GET", "/api/org/members"): Call(200, check=_members_check),
    ("POST", "/api/org/members"): Call(
        201,
        body=lambda w, o: {"github_login": _newcomer(o), "role": "member"},
        shows=lambda o: [f"{o.mark}-newcomer"],
        check=_added_check,
    ),
    ("PATCH", "/api/org/members/{uid}"): Call(
        200, body=lambda w, o: {"role": "owner"}, check=_owner_kept_check
    ),
    ("DELETE", "/api/org/members/{uid}"): Call(409, shows=lambda o: ["last_owner"]),
    ("DELETE", "/api/org/membership"): Call(409, shows=lambda o: ["last_owner"]),
    # Invitations (M2f-1 section 4.8; test_portal_members.py drives them)
    ("GET", "/api/org/invitations"): Call(200, check=_invitations_check),
    ("DELETE", "/api/org/invitations/{uid}"): Call(
        404, shows=lambda o: ["no_such_invitation"]
    ),
    # Deleting the org (test_portal_org_delete.py deletes one for real; the
    # sweep must keep its world, so it sends a wrong slug)
    ("DELETE", "/api/org"): Call(
        409,
        body=lambda w, o: {"confirm_slug": "wrong"},
        shows=lambda o: ["confirm_slug"],
    ),
    # Org settings, ownership and the audit log (M2f-1 sections 4.8, 4.9;
    # test_portal_org_settings.py and test_portal_audit.py drive them; the
    # transfer sends a wrong slug so the sweep keeps its owners)
    ("PATCH", "/api/org"): Call(
        200,
        body=lambda w, o: {"default_project_role": "contributor"},
        check=lambda body, w, o: _org_settings_check(body, o),
    ),
    ("POST", "/api/org/transfer"): Call(
        409,
        body=lambda w, o: {"user_uid": "someone", "confirm_slug": "wrong"},
        shows=lambda o: ["confirm_slug"],
    ),
    ("GET", "/api/org/audit"): Call(
        200, check=lambda body, w, o: _audit_check(body, o)
    ),
    ("GET", "/api/org/audit.csv"): Call(200, shows=lambda o: ["created_at"]),
    # Onboarding (M2f-3 section 4.12): booleans only, no org data to leak
    ("GET", "/api/onboarding"): Call(
        200, check=lambda body, w, o: _onboarding_check(body)
    ),
    ("DELETE", "/api/org/welcome"): Call(204),
    # Monthly budgets (M2f-2 section 4.11; test_portal_budgets.py drives
    # them). The owner may set any member's override, their own included;
    # a DELETE is idempotent.
    ("GET", "/api/budgets"): Call(200, check=_budgets_check),
    ("PUT", "/api/budgets/org"): Call(
        200, body=_budget_body(100), shows=lambda o: ["monthly_usd"]
    ),
    ("DELETE", "/api/budgets/org"): Call(204),
    ("PUT", "/api/budgets/member-default"): Call(
        200, body=_budget_body(10), shows=lambda o: ["monthly_usd"]
    ),
    ("DELETE", "/api/budgets/member-default"): Call(204),
    ("PUT", "/api/budgets/members/{uid}"): Call(
        200, body=_budget_body(10), shows=lambda o: [o.owner_uid]
    ),
    ("DELETE", "/api/budgets/members/{uid}"): Call(204),
    ("PUT", "/api/projects/{slug}/budget"): Call(
        200, body=_budget_body(5), shows=lambda o: [o.name]
    ),
    ("DELETE", "/api/projects/{slug}/budget"): Call(204),
    # Prices (M2f-2 section 4.11): each org's overrides on the bundled table
    ("GET", "/api/prices"): Call(200, shows=lambda o: [o.model], check=_prices_check),
    ("PUT", "/api/prices"): Call(200, body=_price_body, shows=lambda o: [o.model]),
    ("DELETE", "/api/prices"): Call(
        204, query=lambda o: {"provider": "anthropic", "model": o.model}
    ),
    # The usage ledger (M2f-2 section 4.11; test_portal_usage_routes.py
    # drives it): every answer shows only its own org's marked rows
    ("GET", "/api/usage"): Call(
        200, query=lambda o: {"group": "project"}, check=_usage_check
    ),
    ("GET", "/api/usage/calls"): Call(
        200, shows=lambda o: [o.model, f"{o.mark}.py", o.marker_sha]
    ),
    ("GET", "/api/usage.csv"): Call(
        200, shows=lambda o: ["created_at", o.model, o.marker_sha]
    ),
    ("GET", "/api/usage/me"): Call(
        200,
        query=lambda o: {"group": "model"},
        shows=lambda o: [o.model],
        check=_my_usage_check,
    ),
    ("GET", "/api/usage/me/calls"): Call(200, shows=lambda o: [f"{o.mark}.py"]),
    ("GET", "/api/usage/me.csv"): Call(200, shows=lambda o: [o.model]),
    ("GET", "/api/projects/{slug}/usage"): Call(200, check=_project_usage_check),
    # The project Overview (M2f-3 section 4.10): only its own runs and ledger
    ("GET", "/api/projects/{slug}/overview"): Call(200, check=_overview_check),
    # Production's GitHub App import page (swept over prod_world, whose
    # owners have not connected GitHub; test_portal_github_import.py drives it)
    ("POST", "/api/github/app/authorize"): Call(
        200, body=lambda w, o: {}, shows=lambda o: ["/login/oauth/authorize"]
    ),
    ("GET", "/api/github/installations"): Call(
        401, shows=lambda o: ["github_authorization_required"]
    ),
    ("GET", "/api/github/installations/{installation_id}/repos"): Call(
        401, shows=lambda o: ["github_authorization_required"]
    ),
    # A project's connected portals (M2e section 4.4; swept over prod_world,
    # which holds no token: the list is empty and a user's uid is no token's;
    # test_portal_connect.py drives both with tokens)
    ("GET", "/api/projects/{slug}/connections"): Call(200, check=_no_connections),
    ("DELETE", "/api/projects/{slug}/connections/{uid}"): Call(
        404, shows=lambda o: ["no such connection"]
    ),
    # A project's access list (M2f-1 section 4.8; test_portal_access.py
    # drives it): the owner is an org admin, so a grant is refused
    ("GET", "/api/projects/{slug}/access"): Call(200, check=_access_check),
    ("PATCH", "/api/projects/{slug}/access"): Call(
        200, body=lambda w, o: {"restricted": False}
    ),
    ("PUT", "/api/projects/{slug}/access/{user_uid}"): Call(
        409, body=lambda w, o: {"role": "viewer"}, shows=lambda o: ["org_admin"]
    ),
    ("DELETE", "/api/projects/{slug}/access/{user_uid}"): Call(
        404, shows=lambda o: ["no_grant"]
    ),
    ("POST", "/api/projects"): Call(
        201,
        body=lambda w, o: {
            "source": "local",
            "path": str(_marked_repo(w.env, o.mark, "extra")),
        },
        shows=lambda o: [f"{o.mark}/extra"],
    ),
    # Local mode's connect and link (M2e section 4.8; the sweep never reaches
    # a platform: a bad URL and an unknown state / link_id refuse first.
    # test_portal_link.py drives the whole flow against the fake platform)
    ("POST", "/api/platform/connect"): Call(
        422,
        body=lambda w, o: {"platform_url": "not a url"},
        shows=lambda o: ["bad_platform_url"],
    ),
    ("POST", "/api/platform/callback"): Call(
        410,
        body=lambda w, o: {"state": "nostate", "iss": "https://wg.example.com"},
        shows=lambda o: ["connect_expired"],
    ),
    ("GET", "/api/platform/pending/{link_id}"): Call(
        410, shows=lambda o: ["link_expired"]
    ),
    ("DELETE", "/api/platform/pending/{link_id}"): Call(
        410, shows=lambda o: ["link_expired"]
    ),
    # Project management
    # (the root only by name: production sends `root: null`, MODE-1; the
    # local root is asserted in test_projects_carry_their_org_root)
    ("GET", "/api/projects/{slug}"): Call(200, shows=lambda o: [o.name]),
    ("PATCH", "/api/projects/{slug}"): Call(
        200, body=lambda w, o: {"name": o.name}, shows=lambda o: [o.name]
    ),
    ("DELETE", "/api/projects/{slug}"): Call(
        # (a production project is a GitHub one, removed by typing its name)
        200,
        body=lambda w, o: {"confirm_name": o.name},
        check=_deleted_check,
    ),
    ("GET", "/api/projects/{slug}/config"): Call(
        200, shows=lambda o: [o.hint("project_openai")]
    ),
    ("PUT", "/api/projects/{slug}/config"): Call(
        200,
        body=lambda w, o: {"config": {"scan": {"forge": "auto"}}},
        shows=lambda o: [o.hint("project_openai")],
    ),
    ("POST", "/api/projects/{slug}/init"): Call(200, body=lambda w, o: {"agents": []}),
    # Scans
    ("POST", "/api/projects/{slug}/scans"): Call(
        202,
        body=lambda w, o: {"trigger": "manual", "analyze": False},
        check=_new_run_check,
    ),
    ("GET", "/api/projects/{slug}/scans"): Call(200, check=_runs_check),
    ("GET", "/api/projects/{slug}/scans/{run_id}"): Call(
        200, shows=lambda o: [f'"id":{o.run_id}']
    ),
    ("POST", "/api/projects/{slug}/scans/{run_id}/cancel"): Call(
        409, shows=lambda o: [f"run {o.run_id} already ended"]
    ),
    ("GET", "/api/projects/{slug}/scans/{run_id}/events"): Call(200),
    ("GET", "/api/projects/{slug}/scans/{run_id}/log"): Call(
        200, shows=lambda o: [f'"run_id":{o.run_id}']
    ),
    ("GET", "/api/projects/{slug}/scan-estimate"): Call(200),
    # The Explorer router
    ("GET", "/api/projects/{slug}/search"): Call(
        200, query=lambda o: {"q": "fn"}, shows=lambda o: [f"{o.mark}_fn"]
    ),
    ("GET", "/api/projects/{slug}/tree"): Call(200, shows=lambda o: [f"{o.mark}.py"]),
    ("GET", "/api/projects/{slug}/graph/overview"): Call(
        200, shows=lambda o: [f"{o.mark}.py"]
    ),
    ("GET", "/api/projects/{slug}/graph/ego"): Call(
        200, query=_qn, shows=lambda o: [o.qualified_name]
    ),
    ("GET", "/api/projects/{slug}/node"): Call(
        200, query=_qn, shows=lambda o: [o.qualified_name]
    ),
    ("GET", "/api/projects/{slug}/node/rationale"): Call(
        200, query=_qn, shows=lambda o: [o.qualified_name]
    ),
    # The stub generator answers with the root and evidence it was given.
    ("POST", "/api/projects/{slug}/node/rationale"): Call(
        400, query=_qn, shows=lambda o: [str(o.root), o.marker_sha]
    ),
    ("GET", "/api/projects/{slug}/node/evidence"): Call(
        200, query=_qn, shows=lambda o: [o.marker_sha, f"{o.mark} marker commit"]
    ),
    ("GET", "/api/projects/{slug}/history"): Call(
        200, query=lambda o: {"path": f"{o.mark}.py"}, shows=lambda o: [o.marker_sha]
    ),
    ("GET", "/api/projects/{slug}/commit/{sha}"): Call(
        200, shows=lambda o: [o.marker_sha, f"{o.mark} marker commit"]
    ),
    ("GET", "/api/projects/{slug}/pr/{number}"): Call(
        200, shows=lambda o: ["not_found"]
    ),
    ("GET", "/api/projects/{slug}/issue/{number}"): Call(
        200, shows=lambda o: ["not_found"]
    ),
    # The Chat router
    ("GET", "/api/projects/{slug}/chat/providers"): Call(
        200, shows=lambda o: [f"{o.mark}-model"]
    ),
    ("GET", "/api/projects/{slug}/chat/models"): Call(
        200,
        query=lambda o: {"provider": "anthropic"},
        shows=lambda o: [f"{o.mark}-model"],
    ),
    ("GET", "/api/projects/{slug}/chat/sessions"): Call(
        200, shows=lambda o: [o.session_title]
    ),
    ("POST", "/api/projects/{slug}/chat/sessions"): Call(
        201,
        body=lambda w, o: {"title": f"{o.mark} second"},
        shows=lambda o: [f"{o.mark}-model"],
    ),
    ("GET", "/api/projects/{slug}/chat/sessions/{session_id}"): Call(
        200, shows=lambda o: [o.session_title]
    ),
    ("PATCH", "/api/projects/{slug}/chat/sessions/{session_id}"): Call(
        200,
        body=lambda w, o: {"title": f"{o.mark} renamed"},
        shows=lambda o: [f"{o.mark} renamed"],
    ),
    ("DELETE", "/api/projects/{slug}/chat/sessions/{session_id}"): Call(204),
    ("POST", "/api/projects/{slug}/chat/sessions/{session_id}/messages"): Call(
        200, body=lambda w, o: {"content": "hello"}, shows=lambda o: [str(o.root)]
    ),
}
"""How the sweep calls each route in its own org (plan section 5.2 item 2).

Every call is made, side effects included: each sweep case gets a fresh
``two_orgs``, and the two orgs' calls touch two different projects.
"""


def _call(w: World, org: OrgWorld, method: str, path: str, spec: Call):
    return w.client.request(
        method,
        _url(path, org),
        params=spec.query(org) if spec.query else None,
        json=spec.body(w, org) if spec.body else None,
        headers=org.owner,
    )


def test_the_sweep_covers_every_live_route(two_orgs: World) -> None:
    """The collected routes are the running app's, and each has a request."""
    assert _org_scoped_routes(two_orgs.client.app) == API_ROUTES
    assert len(API_ROUTES) > 30
    assert set(ROUTE_REQUESTS) == set(API_ROUTES)
    org_scoped = {
        (m, p)
        for (p, m) in ROUTE_ACTIONS
        if (p, m) not in NON_ORG_ROUTES and (p, m) not in V1_ROUTES
    }
    assert org_scoped == set(API_ROUTES)
    production_org = {(m, p) for (p, m) in PRODUCTION_ORG_ROUTES}
    assert production_org <= set(API_ROUTES)
    assert set(LOCAL_API_ROUTES) == set(API_ROUTES) - production_org


@pytest.mark.parametrize(
    ("method", "path"),
    LOCAL_API_ROUTES,
    ids=[f"{m} {p}" for m, p in LOCAL_API_ROUTES],
)
def test_every_org_scoped_route_is_isolated(
    two_orgs: World, method: str, path: str
) -> None:
    """Items 1, 2 and 7: 404 outside the org; inside it, only its own data."""
    w = two_orgs
    spec = ROUTE_REQUESTS.get((method, path))
    assert spec is not None, f"add a ROUTE_REQUESTS entry for {method} {path}"
    # Items 1 and 7: another org's member, a user with no membership, an
    # unknown org - one indistinguishable 404, before any body is read.
    for headers in (
        w.as_("bob", "local"),
        w.as_("alice", "bravo"),
        w.as_("erin", "local"),
        w.as_("erin", "bravo"),
        w.as_("alice", "nope"),
    ):
        response = w.client.request(method, _url(path, w.local), headers=headers)
        assert response.status_code == 404, (headers, response.text)
        assert response.json() == NOT_FOUND, headers
    # Item 2: the same slug in each org answers with that org's project.
    for org in (w.beta, w.local):
        other = w.other(org)
        response = _call(w, org, method, path, spec)
        where = f"{org.slug}: {method} {path}"
        assert response.status_code == spec.status, (where, response.text)
        skip = (other.mark,) if spec.discovery else ()
        assert_no_leak(response.text, other, where=where, skip=skip)
        for expected in spec.shows(org):
            assert expected.lower() in response.text.lower(), (where, expected)
        if response.status_code == 404:  # a domain 404, not the binding's
            assert response.json() != NOT_FOUND
            assert "project 'api'" not in response.text, where
        if spec.check is not None:
            spec.check(response.json() if response.content else None, w, org)


def test_another_orgs_run_ids_are_not_found_under_the_same_slug(
    two_orgs: World,
) -> None:
    w = two_orgs
    for org in (w.beta, w.local):
        run_id = w.other(org).run_id
        base = f"/api/projects/api/scans/{run_id}"
        for response in (
            w.client.get(base, headers=org.owner),
            w.client.get(f"{base}/events", headers=org.owner),
            w.client.get(f"{base}/log", headers=org.owner),
            w.client.post(f"{base}/cancel", headers=org.owner),
        ):
            assert response.status_code == 404, response.text
            assert response.json() == {"error": f"run {run_id} not found"}
    # Nothing was cancelled or replaced on the other side.
    for org in (w.beta, w.local):
        assert [(r["id"], r["status"]) for r in runs(w.client, org.owner)] == [
            (org.run_id, "ok")
        ]


# ---------------------------------------------------------------------------
# MCP (plan section 5.2 item 3)
# ---------------------------------------------------------------------------

MCP_TOOL_ARGS: dict[str, Callable[[OrgWorld], dict]] = {
    "whygraph_evidence_for": lambda o: {"qualified_name": o.qualified_name},
    "whygraph_area_history": lambda o: {"path": f"{o.mark}.py"},
    "whygraph_rationale_brief": lambda o: {"qualified_name": o.qualified_name},
}
"""Minimal valid arguments per MCP tool; a tool without an entry fails item 3."""


def _mcp(w: World, headers: dict, method: str, params: dict | None = None):
    return w.client.post(
        "/mcp/api", json=_rpc(method, params), headers={**MCP_HEADERS, **headers}
    )


def test_every_mcp_tool_answers_from_its_own_org(two_orgs: World) -> None:
    w = two_orgs
    for org in (w.beta, w.local):
        other = w.other(org)
        listed = _mcp(w, org.owner, "tools/list")
        assert listed.status_code == 200, listed.text
        tools = {t["name"] for t in _sse_json(listed.text)["result"]["tools"]}
        assert tools and tools <= set(MCP_TOOL_ARGS), tools - set(MCP_TOOL_ARGS)
        for tool in sorted(tools):
            response = _mcp(
                w,
                org.owner,
                "tools/call",
                {"name": tool, "arguments": MCP_TOOL_ARGS[tool](org)},
            )
            assert response.status_code == 200, response.text
            result = _sse_json(response.text)["result"]
            # Only the rationale tool fails: on purpose, in the stub generator.
            assert result["isError"] is (tool == "whygraph_rationale_brief"), result
            text = json.dumps(result)
            assert_no_leak(text, other, where=f"{org.slug}: {tool}")
            # Evidence / history list the marker commit; the stub generator
            # names the root and the evidence it was handed.
            assert org.marker_sha in text, (tool, text[:800])
        for uri in (f"whygraph://commit/{org.marker_sha}", "whygraph://repo/overview"):
            read = _mcp(w, org.owner, "resources/read", {"uri": uri})
            assert read.status_code == 200, read.text
            assert_no_leak(read.text, other, where=f"{org.slug}: {uri}")
        commit = _mcp(
            w,
            org.owner,
            "resources/read",
            {"uri": f"whygraph://commit/{org.marker_sha}"},
        )
        assert f"{org.mark} marker commit" in commit.text
        foreign = _mcp(
            w,
            org.owner,
            "resources/read",
            {"uri": f"whygraph://commit/{other.marker_sha}"},
        )
        assert f"{other.mark} marker commit" not in foreign.text
    for headers in (
        w.as_("bob", "local"),
        w.as_("alice", "bravo"),
        w.as_("erin", "local"),
        w.as_("alice", "nope"),
    ):
        response = _mcp(w, headers, "tools/list")
        assert response.status_code == 404, headers
        assert response.json() == NOT_FOUND


# ---------------------------------------------------------------------------
# Org defaults, slugs, the port report (plan section 5.2 items 5, 6, 8)
# ---------------------------------------------------------------------------


def test_org_defaults_and_their_key_clearing_stay_in_their_org(
    two_orgs: World,
) -> None:
    w = two_orgs
    local_before = _ok(w.client.get("/api/portal/defaults", headers=w.local.owner))
    assert local_before["no_provider_key"] is False
    contexts = w.client.app.state.portal.contexts

    gateway = "http://gateway.example/v1"
    changed = w.client.put(
        "/api/portal/defaults",
        json={
            "config": {"llm": {"model": w.beta.model, "openai": {"base_url": gateway}}}
        },
        headers=w.beta.owner,
    )
    # Issue 2: only beta's inheriting project key is cleared.
    assert _ok(changed)["cleared_project_keys"] == [
        {"slug": "api", "provider": "openai"}
    ]
    beta_config = _ok(w.client.get("/api/projects/api/config", headers=w.beta.owner))
    assert beta_config["secrets"]["llm"]["openai"] == {"set": False, "hint": None}
    local_config = _ok(w.client.get("/api/projects/api/config", headers=w.local.owner))
    assert local_config["secrets"]["llm"]["openai"] == {
        "set": True,
        "hint": "…" + w.local.hint("project_openai"),
    }
    assert _ok(w.client.get("/api/portal/defaults", headers=w.local.owner)) == (
        local_before
    )
    local_llm = contexts.get(w.local.project_id).config.llm
    assert local_llm.openai.base_url != gateway
    assert local_llm.openai.api_key == w.local.secrets["project_openai"]
    assert contexts.get(w.beta.project_id).config.llm.openai.base_url == gateway

    # Issue 1: no_provider_key looks at the org's own keys only.
    cleared = w.client.put(
        "/api/portal/defaults",
        json={"secrets": {"llm": {"anthropic": None}}},
        headers=w.beta.owner,
    )
    assert _ok(cleared)["no_provider_key"] is True
    local_after = _ok(w.client.get("/api/portal/defaults", headers=w.local.owner))
    assert local_after == local_before
    assert (
        contexts.get(w.local.project_id).config.llm.anthropic.api_key
        == (w.local.secrets["anthropic"])
    )


def test_slugs_are_unique_per_org(two_orgs: World) -> None:
    w = two_orgs
    for org in (w.local, w.beta):  # local's api-2 does not push beta's to api-3
        root = _marked_repo(w.env, f"{org.mark}-two")
        added = w.client.post(
            "/api/projects",
            json={"source": "local", "path": str(root)},
            headers=org.owner,
        )
        assert _ok(added, 201)["project"]["slug"] == "api-2"
        body = _ok(w.client.get("/api/projects/api-2", headers=org.owner))
        assert body["root"] == str(root)
        listed = _ok(w.client.get("/api/projects", headers=org.owner))["projects"]
        assert sorted(p["slug"] for p in listed) == ["api", "api-2"]
        assert_no_leak(json.dumps(listed), w.other(org), where=org.slug)


def test_the_port_report_is_scoped_to_the_org(two_orgs: World) -> None:
    w = two_orgs

    def item(org: OrgWorld) -> dict:
        return {
            "project_id": org.project_id,
            "org_id": org.org_id,
            "slug": "api",
            "root": str(org.root),
        }

    state = w.client.app.state.portal
    state.port_change = {
        "port": PORT,
        "previous_port": 9999,
        "projects": [item(w.local)],
        "unmounted": [item(w.beta)],
    }
    local = _ok(w.client.get("/api/projects/api", headers=w.local.owner))
    assert local["port_change"] == item(w.local)
    beta = _ok(w.client.get("/api/projects/api", headers=w.beta.owner))
    assert beta["port_change"] == {**item(w.beta), "unmounted": True, "port": PORT}
    local_state = _ok(w.client.get("/api/portal/state", headers=w.local.owner))
    assert local_state["port_change"]["projects"] == [item(w.local)]
    assert local_state["port_change"]["unmounted"] == []
    beta_state = _ok(w.client.get("/api/portal/state", headers=w.beta.owner))
    assert beta_state["port_change"]["projects"] == []
    assert beta_state["port_change"]["unmounted"] == [item(w.beta)]
    for body, other in (
        (local, w.beta),
        (beta, w.local),
        (local_state, w.beta),
        (beta_state, w.local),
    ):
        assert_no_leak(json.dumps(body), other, where="port report")


# ---------------------------------------------------------------------------
# The role matrix over HTTP (plan section 5.3)
# ---------------------------------------------------------------------------

_PROJECT_PATH = "/api/projects/{slug}"
_PROJECT_ACTIONS = {str(a) for a in PROJECT_ACTIONS}
ORG_MEMBER_ACTIONS = {str(a) for a in ROLE_TABLE[Role.MEMBER]}
ORG_ADMIN_ACTIONS = {str(a) for a in ROLE_TABLE[Role.ADMIN]}
VIEWER_ACTIONS = {str(a) for a in PROJECT_ROLE_ACTIONS[ProjectRole.VIEWER]}
CONTRIBUTOR_ACTIONS = {str(a) for a in PROJECT_ROLE_ACTIONS[ProjectRole.CONTRIBUTOR]}
PROJECT_ADMIN_ACTIONS = {str(a) for a in PROJECT_ROLE_ACTIONS[ProjectRole.ADMIN]}
"""Org actions come from the org roles, project actions from the project
roles (M2f-1 plan section 6.3 #2): carol, a member under the default
``contributor``, is a contributor on ``api``; dave, an org admin, is a
project admin on every project."""
_LOCAL_ROUTE_ACTIONS = {
    (m, p): action
    for (p, m), action in ROUTE_ACTIONS.items()
    if action not in NON_ORG_ACTIONS
    and (p, m) not in PRODUCTION_ORG_ROUTES
    and (p, m) not in V1_ROUTES
}
"""Local mode's org-scoped routes and their actions (the members routes are
production-only; their role matrix is ``test_portal_members.py``; the
bearer-only ``V1_ROUTES`` are production-only too)."""


def _routes_doing(actions: set[str]) -> list[tuple[str, str]]:
    """Local routes whose action is in ``actions``, DELETE last."""
    return sorted(
        (route for route, action in _LOCAL_ROUTE_ACTIONS.items() if action in actions),
        key=lambda route: (route[0] == "DELETE", route[1], route[0]),
    )


ORG_ADMIN_ROUTES = _routes_doing(ORG_ADMIN_ACTIONS - ORG_MEMBER_ACTIONS)
PROJECT_ADMIN_ROUTES = _routes_doing(PROJECT_ADMIN_ACTIONS - CONTRIBUTOR_ACTIONS)
ADMIN_ROUTES = sorted(
    ORG_ADMIN_ROUTES + PROJECT_ADMIN_ROUTES,
    # DELETE last, and removing the project last of all (its budget's
    # DELETE needs it).
    key=lambda route: (
        route[0] == "DELETE",
        route == ("DELETE", _PROJECT_PATH),
        route[1],
        route[0],
    ),
)
OWNER_ROUTES = sorted(
    route
    for route, action in _LOCAL_ROUTE_ACTIONS.items()
    if action not in ORG_ADMIN_ACTIONS | _PROJECT_ACTIONS
)
MEMBER_ROUTES = _routes_doing(ORG_MEMBER_ACTIONS | CONTRIBUTOR_ACTIONS)
VIEWER_ROUTES = _routes_doing(VIEWER_ACTIONS)
PROJECT_ROUTES = sorted(
    (route for route in _LOCAL_ROUTE_ACTIONS if route[1].startswith(_PROJECT_PATH)),
    key=lambda route: (route[0] == "DELETE", route[1], route[0]),
)
"""Every local route that names a project, whatever its action."""


def _refusal(action: str, org_role: str, project_role: str) -> dict:
    """The ``403`` body for ``action``: project actions name the project role."""
    if action in _PROJECT_ACTIONS:
        error = f"your role on this project ({project_role}) cannot {action}"
    else:
        error = f"your role ({org_role}) cannot {action}"
    return {"error": error, "code": "forbidden", "action": action}


@pytest.fixture
def context_calls(two_orgs: World, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every ``ContextCache.get`` / ``aget`` from here on (the cache starts empty)."""
    contexts = two_orgs.client.app.state.portal.contexts
    contexts.invalidate()
    calls: list[int] = []
    get, aget = contexts.get, contexts.aget

    def spy_get(project_id: int):  # noqa: ANN202
        calls.append(project_id)
        return get(project_id)

    async def spy_aget(project_id: int):  # noqa: ANN202
        calls.append(project_id)
        return await aget(project_id)

    monkeypatch.setattr(contexts, "get", spy_get)
    monkeypatch.setattr(contexts, "aget", spy_aget)
    return calls


def test_the_admin_routes_are_the_planned_ones() -> None:
    # Org settings and org-level keys are the owner's (M2d-1 plan section 0.1).
    assert OWNER_ROUTES == [
        ("POST", "/api/portal/defaults/keys/{provider}/test"),
        ("PUT", "/api/portal/defaults"),
    ]
    # The org admin's: adding and removing projects (removal is an org
    # action, M2f-1 plan section 0.2 #7), and the budgets (M2f-2 section
    # 0.2 #12; the per-member ones are production's).
    assert set(ORG_ADMIN_ROUTES) == {
        ("POST", "/api/projects"),
        ("POST", "/api/portal/check-path"),
        ("DELETE", "/api/projects/{slug}"),
        ("GET", "/api/budgets"),
        ("PUT", "/api/budgets/org"),
        ("DELETE", "/api/budgets/org"),
        ("PUT", "/api/projects/{slug}/budget"),
        ("DELETE", "/api/projects/{slug}/budget"),
        # Usage and price overrides (M2f-2 section 0.2 #12, #13; a member's
        # own usage is production's)
        ("GET", "/api/usage"),
        ("GET", "/api/usage/calls"),
        ("GET", "/api/usage.csv"),
        ("PUT", "/api/prices"),
        ("DELETE", "/api/prices"),
        # Linking to a platform adds a project, so it is the admin's (M2e
        # section 4.8)
        ("POST", "/api/platform/connect"),
        ("POST", "/api/platform/callback"),
        ("GET", "/api/platform/pending/{link_id}"),
        ("DELETE", "/api/platform/pending/{link_id}"),
        # The first-run checklist (M2f-3 section 4.12)
        ("GET", "/api/onboarding"),
    }
    # The project admin's (local mode; the connections routes are
    # production's).
    assert set(PROJECT_ADMIN_ROUTES) == {
        ("PATCH", "/api/projects/{slug}"),
        ("PUT", "/api/projects/{slug}/config"),
        ("POST", "/api/projects/{slug}/init"),
        ("GET", "/api/projects/{slug}/usage"),  # M2f-2 section 0.2 #13
        # Testing the project's keys (M2f-3 section 4.13)
        ("POST", "/api/projects/{slug}/keys/{provider}/test"),
        ("POST", "/api/projects/{slug}/github-token/test"),
    }
    # Everyone reads the price table (it prices their own estimates).
    assert ("GET", "/api/prices") in MEMBER_ROUTES
    # Generating a card spends: a contributor's, never a viewer's (section 4.6).
    assert ("POST", "/api/projects/{slug}/node/rationale") not in VIEWER_ROUTES
    assert ("POST", "/api/projects/{slug}/node/rationale") in MEMBER_ROUTES
    assert len(VIEWER_ROUTES) > 10


def test_a_member_is_refused_every_admin_action_before_any_context(
    two_orgs: World, context_calls: list[int]
) -> None:
    w = two_orgs
    for method, path in OWNER_ROUTES + ADMIN_ROUTES:
        spec = ROUTE_REQUESTS[(method, path)]
        response = w.client.request(
            method,
            _url(path, w.local),
            params=spec.query(w.local) if spec.query else None,
            json=spec.body(w, w.local) if spec.body else None,
            headers=w.as_("carol", "local"),
        )
        action = ROUTE_ACTIONS[(path, method)]
        assert response.status_code == 403, (method, path, response.text)
        assert response.json() == _refusal(action, "member", "contributor")
        assert context_calls == [], (method, path)  # no secret was decrypted
    # The admin is refused the owner's routes the same way.
    for method, path in OWNER_ROUTES:
        spec = ROUTE_REQUESTS[(method, path)]
        response = w.client.request(
            method,
            _url(path, w.local),
            json=spec.body(w, w.local) if spec.body else None,
            headers=w.as_("dave", "local"),
        )
        action = ROUTE_ACTIONS[(path, method)]
        assert response.status_code == 403, (method, path, response.text)
        assert response.json() == _refusal(action, "admin", "admin")
    assert context_calls == []
    # The admin passes every admin route, in the same order (DELETE last).
    for method, path in ADMIN_ROUTES:
        spec = ROUTE_REQUESTS[(method, path)]
        if path == "/api/projects":  # carol's body made the repo; nothing registered it
            body = {"source": "local", "path": str(w.local.root.parent / "extra")}
        else:
            body = spec.body(w, w.local) if spec.body else None
        response = w.client.request(
            method,
            _url(path, w.local),
            params=spec.query(w.local) if spec.query else None,
            json=body,
            headers=w.as_("dave", "local"),
        )
        assert response.status_code == spec.status, (method, path, response.text)
    assert w.local.project_id in context_calls  # the spy does see a context build


def test_members_and_admins_can_read_chat_scan_and_use_mcp(
    two_orgs: World,
) -> None:
    w = two_orgs
    for user in ("carol", "dave"):
        headers = w.as_(user, "local")
        session = w.client.post(
            "/api/projects/api/chat/sessions",
            json={"title": w.local.session_title},
            headers=headers,
        )
        w.local.session_id = _ok(session, 201)["id"]
        for method, path in MEMBER_ROUTES:
            spec = ROUTE_REQUESTS[(method, path)]
            response = w.client.request(
                method,
                _url(path, w.local),
                params=spec.query(w.local) if spec.query else None,
                json=spec.body(w, w.local) if spec.body else None,
                headers=headers,
            )
            assert response.status_code == spec.status, (user, method, path)
        wait_idle(w.client, headers)
        # A real 2xx cancel: a held quick scan, cancelled while it runs (a
        # full one is the project admin's, test_only_project_admins_...).
        w.scanner.hold.touch()
        quick = {"analyze": False}
        run_id = _ok(
            w.client.post("/api/projects/api/scans", json=quick, headers=headers), 202
        )["run_id"]
        wait_for(
            lambda: (
                next(r for r in runs(w.client, headers) if r["id"] == run_id)["status"]
                == "running"
            )
        )
        cancel = w.client.post(
            f"/api/projects/api/scans/{run_id}/cancel", headers=headers
        )
        assert cancel.status_code == 202, cancel.text
        w.scanner.hold.unlink()
        assert wait_idle(w.client, headers)[0]["status"] == "cancelled"
        mcp = _mcp(w, headers, "tools/list")
        assert mcp.status_code == 200, (user, mcp.text)


def _post_scan(w: World, user: str, body: dict | None) -> httpx.Response:
    return w.client.post(
        "/api/projects/api/scans", json=body, headers=w.as_(user, "local")
    )


def _held_run(w: World, user: str, body: dict | None) -> int:
    """Start a scan as ``user`` and wait until its (held) child runs."""
    headers = w.as_(user, "local")
    run_id = _ok(_post_scan(w, user, body), 202)["run_id"]
    wait_for(
        lambda: (
            next(r for r in runs(w.client, headers) if r["id"] == run_id)["status"]
            == "running"
        )
    )
    return run_id


def test_only_project_admins_start_or_cancel_a_full_scan(two_orgs: World) -> None:
    """The quick / full split (M2f-1 plan sections 4.5 and 6.2 #2), over HTTP."""
    w = two_orgs
    carol, dave = w.as_("carol", "local"), w.as_("dave", "local")
    full_refusal = _refusal("project.scan_full", "member", "contributor")
    # A first scan is structure-only, so a contributor's body-less one runs.
    with portal_db.get_session() as db:
        db.get(Project, w.local.project_id).last_scan_at = None
    first = _ok(_post_scan(w, "carol", None), 202)["run_id"]
    (row,) = [r for r in wait_idle(w.client, carol) if r["id"] == first]
    assert (row["trigger"], row["analyze"], row["status"]) == ("initial", False, "ok")

    for body in (
        None,  # body-less: `manual` = full
        {},
        {"trigger": "manual"},
        {"trigger": "manual", "analyze": True},
        {"trigger": "describe"},
    ):
        response = _post_scan(w, "carol", body)
        assert response.status_code == 403, (body, response.text)
        assert response.json() == full_refusal, body
    assert runs(w.client, carol)[0]["id"] == first  # nothing was queued
    quick = _ok(_post_scan(w, "carol", {"analyze": False}), 202)["run_id"]
    assert [r for r in wait_idle(w.client, carol) if r["id"] == quick][0][
        "analyze"
    ] is False
    full = _ok(_post_scan(w, "dave", None), 202)["run_id"]
    (row,) = [r for r in wait_idle(w.client, dave) if r["id"] == full]
    assert (row["trigger"], row["analyze"], row["status"]) == ("manual", True, "ok")

    # Cancelling needs the action that would start the run (section 0.2 #18).
    w.scanner.hold.touch()
    running = _held_run(w, "dave", {"trigger": "manual"})
    detail = _ok(w.client.get("/api/projects/api", headers=carol))
    assert detail["running_scan"] == {
        "id": running,
        "status": "running",
        "trigger": "manual",
        "analyze": True,
    }
    refused = w.client.post(f"/api/projects/api/scans/{running}/cancel", headers=carol)
    assert refused.status_code == 403, refused.text
    assert refused.json() == full_refusal
    # Carol's quick request, merged into by Dave's full one, is now full.
    queued = _ok(_post_scan(w, "carol", {"analyze": False}), 202)["run_id"]
    assert _ok(_post_scan(w, "dave", {"trigger": "describe"}), 202)["run_id"] == queued
    refused = w.client.post(f"/api/projects/api/scans/{queued}/cancel", headers=carol)
    assert refused.status_code == 403, refused.text
    assert (
        _ok(w.client.post(f"/api/projects/api/scans/{queued}/cancel", headers=dave))[
            "was"
        ]
        == "queued"
    )
    cancelled = w.client.post(f"/api/projects/api/scans/{running}/cancel", headers=dave)
    assert cancelled.status_code == 202, cancelled.text
    w.scanner.hold.unlink()
    wait_idle(w.client, dave)


def _grant_carol(w: World, role: str | None) -> None:
    """Set (or, with ``None``, drop) carol's grant on local's ``api``."""
    carol = w.users["carol"]["x-test-user"]
    with portal_db.get_session() as db:
        user_id = db.exec(select(User.id).where(User.uid == carol)).one()
        grant = db.get(ProjectGrant, (w.local.project_id, user_id))
        if role is None:
            if grant is not None:
                db.delete(grant)
            return
        if grant is None:
            grant = ProjectGrant(
                org_id=w.local.org_id,
                project_id=w.local.project_id,
                user_id=user_id,
                role=role,
            )
        grant.role = role
        db.add(grant)


def _set_local(w: World, *, restricted: bool | None = None, default: str | None = None):
    with portal_db.get_session() as db:
        if restricted is not None:
            db.get(Project, w.local.project_id).restricted = restricted
        if default is not None:
            db.get(Organization, w.local.org_id).default_project_role = default


def _end_frame(text: str) -> dict:
    """The terminal ``end`` frame of a finished run's event stream."""
    frames = [
        line[len("data: ") :] for line in text.splitlines() if line.startswith("data: ")
    ]
    end = json.loads(frames[-1])
    assert end["type"] == "end", end
    return end


def test_run_cost_and_the_estimate_price_are_project_admin_only(
    two_orgs: World,
) -> None:
    """R1 and R6 (M2f-3 plan sections 0.3 #39, #46 and 6.2 #3), per role.

    Every reader sees the run, its requester and its outcome; only a
    ``project.usage`` holder (an owner, an org admin, a project admin) sees
    what it cost or was expected to cost, and the describe estimate's price.
    """
    w = two_orgs
    usage = {"calls": 3, "cost_usd": 0.42, "cost_source": "provider"}
    estimate = {"commits": 4, "model": {}, "cost_usd": 0.6, "missing_key": None}
    with portal_db.get_session() as db:
        run = db.get(ScanRun, w.local.run_id)
        summary = {**json.loads(run.summary), "usage": usage, "estimate": estimate}
        run.summary = json.dumps(summary)
        db.add(run)
    alice = {"uid": w.users["alice"]["x-test-user"], "label": "Alice"}
    base = f"/api/projects/api/scans/{w.local.run_id}"
    for user, grant, sees_cost in (
        ("alice", None, True),  # the owner
        ("dave", None, True),  # an org admin
        ("carol", "admin", True),  # a project admin
        ("carol", None, False),  # a contributor (the org default)
        ("carol", "viewer", False),
    ):
        _grant_carol(w, grant)
        headers = w.as_(user, "local")
        where = (user, grant)
        (listed,) = [r for r in runs(w.client, headers) if r["id"] == w.local.run_id]
        one = _ok(w.client.get(base, headers=headers))
        assert one == listed, where
        assert one["requested_by"] == alice, where
        assert one["status"] == "ok", where
        stream = w.client.get(f"{base}/events", headers=headers)
        assert stream.status_code == 200, stream.text
        end = _end_frame(stream.text)
        assert (end["status"], end["summary"] is not None) == ("ok", True), where
        est = _ok(w.client.get("/api/projects/api/scan-estimate", headers=headers))
        assert {"commits", "upper_bound", "large_commits", "model"} <= set(est)
        assert "missing_key" in est, where
        if sees_cost:
            assert (one["cost_usd"], one["estimate_usd"]) == (0.42, 0.6), where
            assert one["summary"]["usage"] == usage, where
            assert one["summary"]["estimate"] == estimate, where
            assert end["summary"]["usage"] == usage, where
            assert "cost_hidden" not in est, where
            assert est["tokens"] is not None, where
        else:
            assert "cost_usd" not in one and "estimate_usd" not in one, where
            assert "usage" not in one["summary"], where
            assert "estimate" not in one["summary"], where
            assert "usage" not in end["summary"], where
            assert "estimate" not in end["summary"], where
            assert est["cost_hidden"] is True, where
            assert (est["cost"], est["tokens"]) == (None, None), where
    _grant_carol(w, None)


def _sweep_call(w: World, user: str, method: str, path: str) -> httpx.Response:
    spec = ROUTE_REQUESTS[(method, path)]
    return w.client.request(
        method,
        _url(path, w.local),
        params=spec.query(w.local) if spec.query else None,
        json=spec.body(w, w.local) if spec.body else None,
        headers=w.as_(user, "local"),
    )


def test_a_viewer_grant_reads_and_is_refused_the_rest(
    two_orgs: World, context_calls: list[int]
) -> None:
    """The project-role dimension (M2f-1 plan section 6.3 #2): a viewer by grant."""
    w = two_orgs
    _grant_carol(w, "viewer")  # lowers the default contributor
    listed = _ok(w.client.get("/api/projects", headers=w.as_("carol", "local")))
    (api,) = listed["projects"]
    assert (api["my_role"], api["permissions"], api["restricted"]) == (
        "viewer",
        ["project.read"],
        False,
    )
    for method, path in PROJECT_ROUTES:
        action = ROUTE_ACTIONS[(path, method)]
        if action in VIEWER_ACTIONS:
            continue
        response = _sweep_call(w, "carol", method, path)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json() == _refusal(action, "member", "viewer"), (method, path)
    assert context_calls == []  # refused before any secret was decrypted
    for method, path in VIEWER_ROUTES:
        response = _sweep_call(w, "carol", method, path)
        spec = ROUTE_REQUESTS[(method, path)]
        assert response.status_code == spec.status, (method, path, response.text)


def _marker_description(w: World) -> str | None:
    with use_project(manual_ctx(w.local.root, slug="api")), project_session() as db:
        return db.get(Commit, w.local.marker_sha).llm_description


def test_a_viewer_causes_no_llm_spend(
    two_orgs: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero LLM constructions across every project route (M2f-1 plan section 6.2 #3)."""
    import whygraph.analyze as analyze_pkg

    w = two_orgs
    built: list[str] = []

    class _Descriptor:
        @classmethod
        def from_config(cls, config):  # noqa: ANN001, ANN206
            built.append("descriptor")
            raise LlmError("offline in tests")

    class _Generator(_StubRationale):
        @classmethod
        def from_config(cls, config):  # noqa: ANN001, ANN206
            built.append("generator")
            return cls()

    def chat_client(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        built.append("chat")
        return _OfflineChat()

    monkeypatch.setattr(analyze_pkg, "LlmDescriptor", _Descriptor)
    monkeypatch.setattr(mcp_rationale, "RationaleGenerator", _Generator)
    monkeypatch.setattr(serve_chat, "make_chat_client", chat_client)
    # The marker commit loses its description: a read would backfill it.
    with use_project(manual_ctx(w.local.root, slug="api")), project_session() as db:
        db.get(Commit, w.local.marker_sha).llm_description = None
        db.commit()
    _grant_carol(w, "viewer")

    for method, path in PROJECT_ROUTES:
        response = _sweep_call(w, "carol", method, path)
        action = ROUTE_ACTIONS[(path, method)]
        if action in VIEWER_ACTIONS:
            spec = ROUTE_REQUESTS[(method, path)]
            assert response.status_code == spec.status, (method, path, response.text)
        else:  # generation and every chat route among them
            assert response.status_code == 403, (method, path, response.text)
    assert built == []
    assert _marker_description(w) is None  # GET /node/evidence and /history read only
    # The same read by a contributor does try to describe the commit.
    _grant_carol(w, None)
    assert _sweep_call(w, "carol", "GET", f"{_PROJECT_PATH}/node/evidence").is_success
    assert built == ["descriptor"]


def test_an_admin_grant_configures_but_never_removes(two_orgs: World) -> None:
    """A project admin by grant passes the project admin routes, not the org's."""
    w = two_orgs
    _grant_carol(w, "admin")
    detail = _ok(w.client.get("/api/projects/api", headers=w.as_("carol", "local")))
    assert detail["my_role"] == "admin"
    assert "project.configure" in detail["permissions"]
    for method, path in PROJECT_ADMIN_ROUTES:
        response = _sweep_call(w, "carol", method, path)
        spec = ROUTE_REQUESTS[(method, path)]
        assert response.status_code == spec.status, (method, path, response.text)
    # Removing a project stays an org admin's action, whatever the grant.
    removed = _sweep_call(w, "carol", "DELETE", _PROJECT_PATH)
    assert removed.status_code == 403, removed.text
    assert removed.json() == _refusal("org.remove_project", "member", "admin")


@pytest.mark.parametrize("how", ["restricted", "default_none"])
def test_no_access_is_an_unknown_project_on_every_route(
    two_orgs: World, context_calls: list[int], how: str
) -> None:
    """Restricted (or default ``none``) without a grant: 404 like an unknown slug."""
    w = two_orgs
    if how == "restricted":
        _set_local(w, restricted=True)
    else:
        _set_local(w, default="none")
    carol = w.as_("carol", "local")
    assert _ok(w.client.get("/api/projects", headers=carol)) == {"projects": []}
    unknown = w.client.get("/api/projects/nope", headers=carol)
    assert unknown.json() == {"error": "project 'nope' not found"}
    for method, path in PROJECT_ROUTES:
        response = _sweep_call(w, "carol", method, path)
        assert response.status_code == 404, (method, path, response.text)
        assert response.json() == {"error": "project 'api' not found"}, (method, path)
    assert context_calls == []
    # Org admins and owners still see it; for them it is a project like any other.
    for user in ("dave", "alice"):
        (api,) = _ok(w.client.get("/api/projects", headers=w.as_(user, "local")))[
            "projects"
        ]
        assert (api["slug"], api["my_role"]) == ("api", "admin")
        assert api["restricted"] is (how == "restricted")
    # A grant gives the project back, with the granted role.
    _grant_carol(w, "contributor")
    (api,) = _ok(w.client.get("/api/projects", headers=carol))["projects"]
    assert api["my_role"] == "contributor"
    assert _ok(w.client.get("/api/projects/api", headers=carol))["slug"] == "api"
    _grant_carol(w, None)
    assert w.client.get("/api/projects/api", headers=carol).status_code == 404


def test_the_org_default_sets_the_role_of_the_ungranted(two_orgs: World) -> None:
    w = two_orgs
    carol = w.as_("carol", "local")
    expected = {
        "contributor": ["project.read", "project.chat", "project.scan"],
        "viewer": ["project.read"],
    }
    for default, permissions in expected.items():
        _set_local(w, default=default)
        (api,) = _ok(w.client.get("/api/projects", headers=carol))["projects"]
        assert (api["my_role"], api["permissions"]) == (default, permissions)
        state = _ok(w.client.get("/api/portal/state", headers=carol))
        assert state["org"]["default_project_role"] == default
    # The owner's view never changes.
    (api,) = _ok(w.client.get("/api/projects", headers=w.local.owner))["projects"]
    assert api["my_role"] == "admin"
    assert api["permissions"] == [
        "project.read",
        "project.chat",
        "project.scan",
        "project.scan_full",
        "project.configure",
        "project.setup",
        "project.access",
        "project.usage",
    ]


# ---------------------------------------------------------------------------
# System work (plan section 5.4)
# ---------------------------------------------------------------------------


def test_system_scans_get_only_their_own_orgs_secrets(
    two_orgs: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch-up, a poll tick and an id-only describe: each child, its org's keys."""
    w = two_orgs
    seen: list[dict] = []
    real_child_env = runner_mod.child_env

    def spy(config, layer, **kwargs):  # noqa: ANN001, ANN003, ANN202
        env, secrets = real_child_env(config, layer, **kwargs)
        seen.append(
            {
                "db": layer["whygraph_db"],
                "config": json.dumps(dataclasses.asdict(config), default=str),
                "env": env,
                "secrets": secrets,
            }
        )
        return env, secrets

    monkeypatch.setattr(runner_mod, "child_env", spy)
    runner = w.client.app.state.portal.runner

    def rewind() -> None:
        with portal_db.get_session() as session:
            for org in (w.local, w.beta):
                row = session.get(Project, org.project_id)
                row.last_scanned_head = org.first_sha  # HEAD moved since
                session.add(row)

    async def describe() -> None:  # the id-only internal entry, no request
        for org in (w.beta, w.local):
            await runner._request(
                org.project_id,
                kind="scan",
                trigger="describe",
                analyze=True,
                requested_by=None,
                scan_requested=True,
                may_spend=True,
            )

    for start in (runner.catch_up, describe):
        rewind()
        seen.clear()
        w.client.portal.call(start)
        wait_for(lambda: len(seen) == 2)
        for org in (w.local, w.beta):
            wait_idle(w.client, org.owner)
        by_root = {
            org.slug: next(s for s in seen if s["db"].startswith(str(org.root)))
            for org in (w.local, w.beta)
        }
        for org in (w.local, w.beta):
            other, call = w.other(org), by_root[org.slug]
            assert call["env"]["GH_TOKEN"] == org.secrets["github"], start
            assert org.secrets["anthropic"] in call["config"]
            if start is describe:  # analyze: the LLM key reaches the child
                assert call["env"]["ANTHROPIC_API_KEY"] == org.secrets["anthropic"]
            for value in other.secrets.values():
                assert value not in call["config"], (start, org.slug, value)
                assert value not in call["env"].values(), (start, org.slug, value)
                assert value not in call["secrets"], (start, org.slug, value)
        for call in w.scanner.calls()[-2:]:  # what the children really got
            child_root = call["cwd"]
            org = w.local if child_root == str(w.local.root) else w.beta
            leaked = set(w.other(org).secrets.values()) & set(call["env"].values())
            assert not leaked, (start, org.slug, leaked)


def test_nothing_answers_signed_out_in_production(
    production_env: SimpleNamespace,
) -> None:
    """Section 5.4 item 3 on a real production portal (M2c sessions and hosts).

    Production makes projects only by a GitHub App import, so the project is
    inserted directly with ``initialized_at`` set and its DB created as the
    import would. The hook-refusal half of this case lives in
    ``test_portal_hosts_isolation.py`` (M2c plan section 7, step 5).
    """
    env = production_env
    root = _marked_repo(env, "narwhal")
    _seed_codegraph(root, "narwhal")
    with use_project(manual_ctx(root, slug="api")):
        ensure_initialized()
    with prod_portal() as client:
        state = client.app.state.portal
        assert state.mode == "production" and state.builtin_org_slug is None
        # After the start, so its catch-up never sees the project.
        with portal_db.get_session() as session:
            beta_id = create_org(session, slug="bravo", name="Beta").id
            bob = User(display_name="Bob", email="bob@example.com")
            session.add(bob)
            session.flush()
            add_member(session, org_id=beta_id, user_id=bob.id, role="owner")
            session.add(
                Project(
                    org_id=beta_id,
                    slug="api",
                    name="Narwhal API",
                    source="local",
                    root=str(root),
                    initialized_at="2026-10-03T00:00:00+00:00",
                    created_by=bob.id,
                )
            )
        bravo = at("bravo")
        # Signed out: every org route is a 401, MCP and setup do not exist.
        # (the local-only routes are 404 in production, before current_user)
        for method, path in API_ROUTES:
            if (path, method) in LOCAL_ONLY_ROUTES:
                continue
            org = SimpleNamespace(
                run_id=1, marker_sha="abc", session_id=1, owner_uid="x"
            )
            url = _url(path, org)
            response = client.request(method, bravo + url)
            assert response.status_code == 401, (method, path, response.text)
            assert response.json() == LOGIN_REQUIRED, (method, path)
        mcp = client.post(
            bravo + "/mcp/api", json=_rpc("tools/list"), headers=MCP_HEADERS
        )
        assert mcp.status_code == 404
        assert mcp.json() == {"error": "no MCP endpoint /mcp/api"}
        setup = client.post(bravo + "/api/portal/setup", json={"display_name": "Eve"})
        assert setup.status_code == 404
        body = _ok(client.get(bravo + "/api/portal/state"))
        assert body["user"] is None and body["org"] is None
