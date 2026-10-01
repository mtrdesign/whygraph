"""Tests for the portal app - :func:`whygraph.portal.app.create_portal_app`.

Each test drives a ``TestClient`` at ``http://127.0.0.1:<port>`` (the
guard's ``Host`` allowlist rejects Starlette's default ``testserver``),
with an isolated data dir and one shared folder holding throwaway git
repos. The MCP transport has its own module, ``test_portal_mcp.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from importlib.metadata import version as package_version
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from alembic import command
from click.testing import CliRunner
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlmodel import select

from conftest import build_fake_codegraph_db
from whygraph.core.config import Config
from whygraph.core.context import ProjectContext, is_strict, use_project
from whygraph.db import bootstrap
from whygraph.db import engine as db_engine
from whygraph.hooks import managed_hook_names
from whygraph.portal import db as portal_db
from whygraph.portal import repos as portal_repos
from whygraph.portal.app import PortalStartupError, create_portal_app
from whygraph.portal.deps import current_user
from whygraph.portal.models import Project, ScanRun
from whygraph.serve import chat as serve_chat
from whygraph.services.github import RepoAccess, RepoAccessError
from whygraph.services.llm.chat import TextDelta, TurnDone

PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"
CLIENT_HEADER = {"X-WhyGraph-Client": "1"}

_NODES = [
    {
        "id": "n_file",
        "kind": "file",
        "name": "sample.py",
        "qualified_name": "sample.py",
        "file_path": "sample.py",
        "language": "python",
        "start_line": 1,
        "end_line": 3,
        "docstring": None,
        "signature": None,
    },
    {
        "id": "n_fn",
        "kind": "function",
        "name": "fn",
        "qualified_name": "sample.fn",
        "file_path": "sample.py",
        "language": "python",
        "start_line": 1,
        "end_line": 3,
        "docstring": None,
        "signature": "def fn()",
    },
]
_EDGES = [("n_file", "n_fn", "contains")]


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def make_repo(parent: Path, name: str, *, remote: str | None = None) -> Path:
    """A git repo with ``sample.py`` committed across two commits."""
    root = parent / name
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "sample.py").write_text("line one\nline two\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-q", "-m", "first commit")
    (root / "sample.py").write_text("line one\nline two\nline three\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-q", "-m", "second commit")
    if remote:
        _git(root, "remote", "add", "origin", remote)
    return Path(os.path.realpath(root))


def manual_ctx(root: Path, slug: str = "manual") -> ProjectContext:
    """A context over ``root``'s default DB paths, for seeding test data."""
    return ProjectContext(
        slug=slug,
        root=root,
        config=Config(
            whygraph_db=root / ".whygraph" / "whygraph.db",
            codegraph_db=root / ".codegraph" / "codegraph.db",
        ),
    )


def seed_codegraph(root: Path) -> None:
    (root / ".codegraph").mkdir(exist_ok=True)
    build_fake_codegraph_db(
        root / ".codegraph" / "codegraph.db", nodes=_NODES, edges=_EDGES
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """An isolated data dir, one shared folder, no ambient keys or modes."""
    data = tmp_path / "data"
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setenv("WHYGRAPH_DATA", str(data))
    monkeypatch.setenv("WHYGRAPH_SHARED_FOLDERS", str(shared))
    for var in (
        "WHYGRAPH_MODE",
        "WHYGRAPH_DEV_ORIGINS",
        "WHYGRAPH_CONFIG_JSON",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("whygraph.serve.app._STATIC_DIR", tmp_path / "nostatic")
    portal_db._reset_engine()
    return SimpleNamespace(
        data=data, shared=Path(os.path.realpath(shared)), tmp=tmp_path
    )


@contextmanager
def portal_client(port: int = PORT) -> Iterator[TestClient]:
    app = create_portal_app(port=port)
    with TestClient(
        app, base_url=f"http://127.0.0.1:{port}", headers=CLIENT_HEADER
    ) as client:
        yield client


@pytest.fixture
def client(env: SimpleNamespace) -> Iterator[TestClient]:
    with portal_client() as c:
        yield c


@pytest.fixture
def ready(client: TestClient) -> TestClient:
    """A portal past first-run setup."""
    response = client.post("/api/portal/setup", json={"display_name": "Tess"})
    assert response.status_code == 201, response.text
    return client


def add_local(client: TestClient, root: Path, **extra) -> dict:
    response = client.post(
        "/api/projects", json={"source": "local", "path": str(root), **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()


def init_project(client: TestClient, slug: str, **body) -> dict:
    response = client.post(f"/api/projects/{slug}/init", json={"agents": [], **body})
    assert response.status_code == 200, response.text
    return response.json()


def initialized_repo(client: TestClient, env: SimpleNamespace, name: str) -> Path:
    root = make_repo(env.shared, name)
    seed_codegraph(root)
    slug = add_local(client, root)["project"]["slug"]
    assert init_project(client, slug)["initialized"] is True
    return root


# ---------------------------------------------------------------------------
# Setup, lifespan, degraded mode
# ---------------------------------------------------------------------------


def test_setup_flow(client: TestClient, env: SimpleNamespace) -> None:
    state = client.get("/api/portal/state").json()
    assert state == {
        "mode": "local",
        "setup_complete": False,
        "user": None,
        "port": PORT,
        "shared_folders": [str(env.shared)],
        "version": package_version("whygraph"),  # what `whygraph version` prints
        "port_change": None,
    }
    assert client.get("/api/projects").json() == {"error": "setup required"}

    assert (
        client.post("/api/portal/setup", json={"display_name": " "}).status_code == 422
    )
    created = client.post("/api/portal/setup", json={"display_name": "Tess"})
    assert created.status_code == 201
    assert created.json()["user"]["display_name"] == "Tess"

    state = client.get("/api/portal/state").json()
    assert state["setup_complete"] is True
    assert state["user"]["display_name"] == "Tess"
    assert client.get("/api/projects").json() == {"projects": []}
    again = client.post("/api/portal/setup", json={"display_name": "Other"})
    assert again.status_code == 409


def test_mode_is_stored_at_first_start_not_at_setup(env: SimpleNamespace) -> None:
    from whygraph.portal.models import Setting

    with portal_client():
        pass
    with portal_db.get_session() as session:
        assert session.get(Setting, 1).mode == "local"


def test_a_mode_change_refuses_to_start(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_MODE", "local")
    with portal_client():
        pass
    monkeypatch.setenv("WHYGRAPH_MODE", "production")
    with pytest.raises(PortalStartupError, match="cannot be changed"):
        with portal_client():
            pass


def test_strict_mode_is_on_for_the_lifespan_only(env: SimpleNamespace) -> None:
    assert is_strict() is False
    with portal_client() as client:
        assert is_strict() is True
        state = client.app.state.portal
        assert not state.shutdown_event.is_set()
    assert is_strict() is False
    # Exit ends open streams and releases the MCP session manager.
    assert state.shutdown_event.is_set()
    assert state.session_manager is None


def test_a_failing_portal_migration_serves_a_degraded_app(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(portal_db, "ensure_initialized", _boom)
    with portal_client() as client:
        state = client.get("/api/portal/state")
        assert state.status_code == 200
        assert "disk on fire" in state.json()["error"]
        assert client.get("/api/projects").status_code == 503
        assert (
            client.post("/api/portal/setup", json={"display_name": "x"}).status_code
            == 503
        )


def test_stale_running_runs_are_marked_interrupted_at_start(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "demo")
    add_local(ready, root)
    with portal_db.get_session() as session:
        project = session.exec(select(Project)).one()
        session.add(ScanRun(project_id=project.id, trigger="manual", status="running"))
    with portal_client():
        pass
    with portal_db.get_session() as session:
        assert session.exec(select(ScanRun)).one().status == "interrupted"


# ---------------------------------------------------------------------------
# The guard: Host / Origin / Sec-Fetch-Site / custom header
# ---------------------------------------------------------------------------


def test_every_api_request_needs_the_client_header(ready: TestClient) -> None:
    bare = {"X-WhyGraph-Client": ""}
    # A cross-origin fetch POST without the custom header ...
    assert ready.post("/api/projects", json={}, headers=bare).status_code == 403
    # ... and an <img>-style cross-site GET that would run an LLM backfill.
    response = ready.get("/api/projects/demo/history?path=a.py", headers=bare)
    assert response.status_code == 403
    assert response.json() == {"error": "missing x-whygraph-client header"}
    assert ready.get("/api/portal/state", headers=bare).status_code == 403


@pytest.mark.parametrize("path", ["/api/portal/state", "/mcp/demo", "/"])
def test_a_foreign_host_is_rejected(ready: TestClient, path: str) -> None:
    response = ready.get(path, headers={"host": "evil.example:8765"})
    assert response.status_code == 421


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://localhost:3000"},
        {"Origin": "null"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Origin": "https://127.0.0.1:8765"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
@pytest.mark.parametrize("path", ["/api/portal/state", "/mcp/demo"])
def test_cross_origin_requests_are_rejected(
    ready: TestClient, path: str, headers: dict
) -> None:
    response = ready.post(path, headers=headers, json={})
    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://127.0.0.1:8765"},
        {"Origin": "http://localhost:8765"},
        {"Origin": "http://[::1]:8765"},
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
    ],
)
def test_same_origin_requests_pass(ready: TestClient, headers: dict) -> None:
    assert ready.get("/api/portal/state", headers=headers).status_code == 200


@pytest.mark.parametrize("host", ["localhost:8765", "[::1]:8765"])
def test_every_loopback_host_is_accepted(ready: TestClient, host: str) -> None:
    assert ready.get("/api/portal/state", headers={"host": host}).status_code == 200


def test_dev_origins_are_added(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WHYGRAPH_DEV_ORIGINS", "http://localhost:5173, bogus")
    with portal_client() as client:
        ok = client.get(
            "/api/portal/state",
            headers={"Origin": "http://localhost:5173", "host": "localhost:5173"},
        )
        assert ok.status_code == 200
        state = client.app.state.portal
        assert "http://localhost:5173" in state.origins.origins
        assert "bogus" not in {o.split("//")[-1] for o in state.origins.origins}


def test_no_cors_headers_are_ever_sent(ready: TestClient) -> None:
    response = ready.options(
        "/api/projects",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


# ---------------------------------------------------------------------------
# Routing: the current_user seam, 404s, unscoped paths
# ---------------------------------------------------------------------------


def _calls(dependant) -> Iterator:
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def _api_routes(app) -> list:
    return [
        rc
        for rc in iter_route_contexts(app.routes)
        if (rc.path or "").startswith("/api") and getattr(rc, "dependant", None)
    ]


def test_every_api_route_resolves_current_user_except_state_and_setup(
    client: TestClient,
) -> None:
    exempt = {("/api/portal/state", "GET"), ("/api/portal/setup", "POST")}
    routes = _api_routes(client.app)
    assert len(routes) > 30
    seen_exempt = set()
    for rc in routes:
        uses_user = current_user in set(_calls(rc.dependant))
        keys = {(rc.path, m) for m in rc.methods}
        if keys & exempt:
            seen_exempt |= keys & exempt
            assert not uses_user, rc.path
        else:
            assert uses_user, f"{rc.path} {rc.methods} does not resolve current_user"
    assert seen_exempt == exempt


def test_every_api_route_answers_setup_required_before_setup(
    client: TestClient,
) -> None:
    fill = {
        "{slug}": "demo",
        "{rest:path}": "x/y",
        "{session_id}": "1",
        "{sha}": "abc",
        "{number}": "1",
        "{run_id}": "1",
    }
    exempt = {("/api/portal/state", "GET"), ("/api/portal/setup", "POST")}
    checked = 0
    for rc in _api_routes(client.app):
        url = rc.path
        for placeholder, value in fill.items():
            url = url.replace(placeholder, value)
        assert "{" not in url, url
        for method in rc.methods:
            if (rc.path, method) in exempt or method == "HEAD":
                continue
            response = client.request(method, url, json={})
            assert response.status_code == 409, (method, url, response.text)
            assert response.json() == {"error": "setup required"}, (method, url)
            checked += 1
    assert checked > 30


def test_unscoped_api_paths_are_json_404s_not_the_spa(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = env.tmp / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>spa</html>")
    monkeypatch.setattr("whygraph.serve.app._STATIC_DIR", static)
    with portal_client() as client:
        tree = client.get("/api/tree")
        assert tree.status_code == 404
        assert tree.headers["content-type"].startswith("application/json")
        assert "error" in tree.json()
        assert client.get("/api").status_code == 404
        for path in ("/mcp", "/mcp/demo/extra", "/mcp/"):
            response = client.post(path, json={})
            assert response.status_code == 404, path
            assert response.headers["content-type"].startswith("application/json")
        # Client routes still fall back to the SPA.
        assert client.get("/p/demo/chat/3").text == "<html>spa</html>"


def test_unknown_project_slug_is_404(ready: TestClient) -> None:
    for path in ("/api/projects/nope", "/api/projects/Bad_Slug/tree"):
        response = ready.get(path)
        assert response.status_code == 404
        assert "error" in response.json()


# ---------------------------------------------------------------------------
# Adding local projects
# ---------------------------------------------------------------------------


def test_add_local_writes_nothing_and_detects_state(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "My Repo_v2", remote="https://github.com/o/r.git")
    before = _git(root, "status", "--porcelain", "--ignored")

    body = add_local(ready, root)

    project = body["project"]
    assert project["slug"] == "my-repo-v2"
    assert project["initialized"] is False
    assert project["stats"] is None
    # (a global `url.<base>.insteadOf` may rewrite the URL's form)
    assert "github.com" in project["remote_url"] and "o/r" in project["remote_url"]
    assert body["detected"] == {
        "existing_db": False,
        "managed_hooks": [],
        "detected_agents": [],
        "custom_db_paths": [],
    }
    assert _git(root, "status", "--porcelain", "--ignored") == before
    assert not (root / ".whygraph").exists()
    # A GitHub-linked repo gets [scan].forge = "auto".
    config = ready.get("/api/projects/my-repo-v2/config").json()["config"]
    assert config["scan"]["forge"] == "auto"


def test_add_local_rejects_paths_outside_shared_folders(
    ready: TestClient, env: SimpleNamespace
) -> None:
    outside = make_repo(env.tmp / "elsewhere", "demo")
    response = ready.post(
        "/api/projects", json={"source": "local", "path": str(outside)}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "not_shared"
    assert body["command"] == f"whygraph up --add-folder {outside.parent}"

    plain = env.shared / "plain"
    plain.mkdir()
    response = ready.post("/api/projects", json={"source": "local", "path": str(plain)})
    assert response.json()["code"] == "not_git"

    inside = make_repo(env.shared, "demo")
    add_local(ready, inside)
    again = ready.post("/api/projects", json={"source": "local", "path": str(inside)})
    assert again.status_code == 409
    assert again.json()["code"] == "duplicate"


def test_slug_collisions_get_a_suffix(ready: TestClient, env: SimpleNamespace) -> None:
    first = make_repo(env.shared / "a", "demo")
    second = make_repo(env.shared / "b", "demo")
    assert add_local(ready, first)["project"]["slug"] == "demo"
    assert add_local(ready, second)["project"]["slug"] == "demo-2"


def test_import_keeps_the_allowlist_and_moves_secrets(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "demo")
    custom_db = root / "custom.db"
    custom_db.write_bytes(b"")
    (root / "whygraph.toml").write_text(
        'whygraph_db = "custom.db"\n'
        "[llm]\n"
        'model = "openai/gpt-4o"\n'
        "[llm.openai]\n"
        'base_url = "http://attacker.example"\n'
        'api_key = "sk-imported-key-9876"\n'
        "[logging]\n"
        'file = "/tmp/x.log"\n'
        "[analyze]\n"
        "max_diff_chars = 1234\n"
        "[scan]\n"
        'provider = "github"\n'
        'hooks = ["post-commit"]\n'
        'token = "ghp_imported_5555"\n'
    )
    response = ready.post("/api/projects", json={"source": "local", "path": str(root)})
    assert response.status_code == 201
    assert "sk-imported-key-9876" not in response.text
    assert "ghp_imported_5555" not in response.text
    body = response.json()
    report = body["import"]
    assert {d["key"] for d in report["dropped"]} == {
        "llm.openai.base_url",
        "whygraph_db",
        "logging",
    }
    assert sorted(report["secrets_moved"]) == ["llm.openai.api_key", "scan.token"]
    custom = body["detected"]["custom_db_paths"]
    assert custom == [
        {
            "key": "whygraph_db",
            "path": str(custom_db),
            "exists": True,
            "message": custom[0]["message"],
        }
    ]
    assert str(custom_db) in custom[0]["message"]

    config = ready.get("/api/projects/demo/config").json()
    assert config["config"] == {
        "llm": {"model": "openai/gpt-4o"},
        "analyze": {"max_diff_chars": 1234},
        "scan": {"forge": "github", "hooks": ["post-commit"]},
    }
    assert config["secrets"]["llm"]["openai"] == {"set": True, "hint": "…9876"}
    assert config["secrets"]["github_token"] == {"set": True, "hint": "…5555"}
    # The report is shown again while the file still has the keys.
    assert {d["key"] for d in config["import"]["dropped"]} == {
        "llm.openai.base_url",
        "whygraph_db",
        "logging",
    }
    # The context never sees the imported endpoint, and DB paths are forced.
    ctx = ready.app.state.portal.contexts.get(_project_id("demo"))
    assert ctx.config.llm.openai.base_url is None
    assert ctx.config.whygraph_db == root / ".whygraph" / "whygraph.db"
    # And the repo file was not modified.
    assert "attacker.example" in (root / "whygraph.toml").read_text()


def _project_id(slug: str) -> int:
    with portal_db.get_session() as session:
        return session.exec(select(Project.id).where(Project.slug == slug)).one()


def test_repo_discovery_and_check_path(ready: TestClient, env: SimpleNamespace) -> None:
    make_repo(env.shared, "one")
    make_repo(env.shared / "deep" / "er", "two")
    repos = ready.get("/api/portal/repos").json()["repos"]
    assert {r["name"] for r in repos} == {"one", "two"}
    assert {
        r["name"] for r in ready.get("/api/portal/repos?q=TWO").json()["repos"]
    } == {"two"}

    shared_repo = str(env.shared / "one")
    check = ready.post("/api/portal/check-path", json={"path": shared_repo}).json()
    assert check["shared"] is True and check["is_git"] is True
    assert check["command"] is None

    escape = ready.post(
        "/api/portal/check-path", json={"path": f"{env.shared}/../elsewhere/x"}
    ).json()
    assert escape["shared"] is False
    assert escape["command"].startswith("whygraph up --add-folder ")
    rel = ready.post("/api/portal/check-path", json={"path": "relative/x"})
    assert rel.status_code == 422


# ---------------------------------------------------------------------------
# Config and secrets (plan section 4.2.1)
# ---------------------------------------------------------------------------


def test_put_config_rejects_secrets_without_echoing_them(
    ready: TestClient, env: SimpleNamespace
) -> None:
    add_local(ready, make_repo(env.shared, "demo"))
    response = ready.put(
        "/api/projects/demo/config",
        json={"config": {"llm": {"anthropic": {"api_key": "sk-super-secret-1"}}}},
    )
    assert response.status_code == 422
    assert "sk-super-secret-1" not in response.text
    assert response.json()["keys"] == ["llm.anthropic.api_key"]

    wrong_type = ready.put(
        "/api/projects/demo/config",
        json={"secrets": {"llm": {"anthropic": 12345678}}},
    )
    assert wrong_type.status_code == 422
    assert "12345678" not in wrong_type.text


def test_put_config_enforces_the_allowlist(
    ready: TestClient, env: SimpleNamespace
) -> None:
    add_local(ready, make_repo(env.shared, "demo"))
    for bad in (
        {"whygraph_db": "/data/portal.db"},
        {"logging": {"file": "/tmp/x"}},
        {"llm": {"claude_cli": {"config_dir": "/root"}}},
    ):
        response = ready.put("/api/projects/demo/config", json={"config": bad})
        assert response.status_code == 422, bad
    invalid = ready.put(
        "/api/projects/demo/config", json={"config": {"analyze": {"max_workers": 0}}}
    )
    assert invalid.status_code == 422
    ok = ready.put(
        "/api/projects/demo/config",
        json={
            "config": {"llm": {"ollama": {"host": "http://host.docker.internal:11434"}}}
        },
    )
    assert ok.status_code == 200
    assert ok.json()["config"]["llm"]["ollama"]["host"].startswith("http://host.docker")


def test_endpoint_change_clears_the_project_key_and_blocks_the_global_one(
    ready: TestClient, env: SimpleNamespace
) -> None:
    add_local(ready, make_repo(env.shared, "demo"))
    ready.put(
        "/api/portal/defaults", json={"secrets": {"llm": {"openai": "sk-global-1111"}}}
    )
    ready.put(
        "/api/projects/demo/config",
        json={"secrets": {"llm": {"openai": "sk-project-2222"}}},
    )
    contexts = ready.app.state.portal.contexts
    pid = _project_id("demo")
    assert contexts.get(pid).config.llm.openai.api_key == "sk-project-2222"

    response = ready.put(
        "/api/projects/demo/config",
        json={"config": {"llm": {"openai": {"base_url": "http://gateway.example/v1"}}}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["config"]["llm"]["openai"]["base_url"] == "http://gateway.example/v1"
    assert body["secrets"]["llm"]["openai"]["set"] is False
    config = contexts.get(pid).config
    assert config.llm.openai.base_url == "http://gateway.example/v1"
    assert (
        config.llm.openai.api_key is None
    )  # no global key into an overridden endpoint


def test_config_and_secret_writes_invalidate_the_context(
    ready: TestClient, env: SimpleNamespace
) -> None:
    add_local(ready, make_repo(env.shared, "demo"))
    assert ready.get("/api/projects/demo").json()["missing_key"] == "anthropic"
    ready.put(
        "/api/portal/defaults", json={"secrets": {"llm": {"anthropic": "sk-a-0001"}}}
    )
    assert ready.get("/api/projects/demo").json()["missing_key"] is None
    ready.put(
        "/api/projects/demo/config",
        json={"config": {"llm": {"model": "openai/gpt-4o"}}},
    )
    assert ready.get("/api/projects/demo").json()["missing_key"] == "openai"
    ready.put(
        "/api/projects/demo/config", json={"secrets": {"llm": {"openai": "sk-o-0002"}}}
    )
    assert ready.get("/api/projects/demo").json()["missing_key"] is None
    ready.put("/api/projects/demo/config", json={"secrets": {"llm": {"openai": None}}})
    assert ready.get("/api/projects/demo").json()["missing_key"] == "openai"


def test_defaults_hold_only_llm_and_task_tables(ready: TestClient) -> None:
    assert ready.get("/api/portal/defaults").json()["no_provider_key"] is True
    bad = ready.put(
        "/api/portal/defaults", json={"config": {"scan": {"forge": "auto"}}}
    )
    assert bad.status_code == 422
    assert bad.json()["keys"] == ["scan"]
    ok = ready.put(
        "/api/portal/defaults",
        json={
            "config": {
                "llm": {"model": "anthropic/claude-x"},
                "chat": {"max_tool_rounds": 3},
            },
            "secrets": {"llm": {"anthropic": "sk-ant-4321"}},
        },
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["no_provider_key"] is False
    assert body["secrets"]["llm"]["anthropic"] == {"set": True, "hint": "…4321"}
    assert "sk-ant-4321" not in ok.text


def test_hooks_change_resyncs_hooks_on_an_initialized_project(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = initialized_repo(ready, env, "demo")
    assert set(managed_hook_names(root)) == {
        "post-commit",
        "post-merge",
        "post-rewrite",
        "post-checkout",
    }
    response = ready.put(
        "/api/projects/demo/config",
        json={"config": {"scan": {"hooks": ["post-commit"]}}},
    )
    assert response.status_code == 200
    assert response.json()["hooks"]["installed"] == ["post-commit"]
    assert managed_hook_names(root) == ("post-commit",)


# ---------------------------------------------------------------------------
# Initialize, gates, migrations
# ---------------------------------------------------------------------------


def test_uninitialized_project_gates_data_routes_and_creates_no_db(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "demo")
    add_local(ready, root)
    for path in ("/api/projects/demo/tree", "/api/projects/demo/chat/sessions"):
        response = ready.get(path)
        assert response.status_code == 409
        assert response.json() == {"error": "not initialized"}
    mcp = ready.post(
        "/mcp/demo",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert mcp.status_code == 409
    assert ready.post("/api/projects/demo/scans").status_code == 409

    assert ready.get("/api/projects/demo/config").status_code == 200
    assert (
        ready.put(
            "/api/projects/demo/config",
            json={"config": {"chat": {"max_tool_rounds": 4}}},
        ).status_code
        == 200
    )
    plan = init_project(ready, "demo", agents=["claude"], dry_run=True)
    assert plan["initialized"] is False
    assert [f["status"] for f in plan["agent_files"]] == ["write"]
    assert not (root / ".whygraph").exists()
    assert not (root / ".mcp.json").exists()

    done = init_project(ready, "demo", agents=["claude"])
    assert done["initialized"] is True and done["marker_written"] is True
    assert (root / ".whygraph" / "whygraph.db").is_file()
    assert not (root / "whygraph.toml").exists()  # acceptance #4: config is portal-side
    assert json.loads((root / ".whygraph" / "portal.json").read_text()) == {
        "slug": "demo",
        "port": PORT,
    }
    entry = json.loads((root / ".mcp.json").read_text())["mcpServers"]["whygraph"]
    assert entry == {
        "type": "http",
        "url": "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/demo",
    }
    details = ready.get("/api/projects/demo").json()
    assert details["agents"] == ["claude"]
    assert details["stats"]["commits"] == 0
    assert ready.get("/api/projects/demo/chat/sessions").json() == []


def test_project_details_recompute_detected_and_last_scan_status(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "demo")
    (root / ".mcp.json").write_text(
        '{"mcpServers": {"whygraph": {"command": "whygraph-mcp"}}}\n'
    )
    added = add_local(ready, root)
    assert added["project"]["detected"] == added["detected"]
    details = ready.get("/api/projects/demo").json()
    assert details["detected"] == added["detected"]
    assert details["detected"]["detected_agents"][0]["agent"] == "claude"
    assert details["last_scan_status"] is None

    # Recomputed on every read (a new tab's Initialize step sees the truth).
    (root / ".mcp.json").unlink()
    (root / ".whygraph").mkdir()
    (root / ".whygraph" / "whygraph.db").touch()
    detected = ready.get("/api/projects/demo").json()["detected"]
    assert detected["detected_agents"] == [] and detected["existing_db"] is True

    pid = _project_id("demo")
    with portal_db.get_session() as session:
        session.add(ScanRun(project_id=pid, trigger="manual", status="failed"))
        session.add(ScanRun(project_id=pid, trigger="manual", status="queued"))
    (listed,) = ready.get("/api/projects").json()["projects"]
    assert listed["last_scan_status"] == "failed"  # a queued run has not ended
    with portal_db.get_session() as session:
        session.add(ScanRun(project_id=pid, trigger="hook", status="ok"))
    assert ready.get("/api/projects/demo").json()["last_scan_status"] == "ok"


def test_init_waits_for_tracked_agent_files(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "demo")
    (root / ".mcp.json").write_text('{"mcpServers": {"other": {"command": "x"}}}\n')
    _git(root, "add", ".mcp.json")
    _git(root, "commit", "-q", "-m", "mcp")
    body = add_local(ready, root)
    assert body["detected"]["detected_agents"] == []

    pending = init_project(ready, "demo", agents=["claude"])
    assert pending["needs_confirmation"] == [".mcp.json"]
    assert pending["initialized"] is False
    assert ready.get("/api/projects/demo/tree").status_code == 409

    done = init_project(ready, "demo", agents=["claude"], confirm_tracked=[".mcp.json"])
    assert done["initialized"] is True
    servers = json.loads((root / ".mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"other", "whygraph"}


def test_an_older_revision_db_opens_cleanly_with_a_backup(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "demo")
    old_revision = "4e231ec6f0e1"  # before the chat tables
    with use_project(manual_ctx(root)):
        (root / ".whygraph").mkdir()
        command.upgrade(bootstrap.alembic_config(), old_revision)
    db_engine._reset_engine()

    body = add_local(ready, root)
    assert body["detected"]["existing_db"] is True
    with portal_db.get_session() as session:
        project = session.exec(select(Project)).one()
        project.initialized_at = "2026-01-01T00:00:00+00:00"
        session.add(project)

    response = ready.get("/api/projects/demo/chat/sessions")
    assert response.status_code == 200, response.text
    assert response.json() == []
    backup = root / ".whygraph" / "backups" / f"whygraph-{old_revision}.db"
    assert backup.is_file()
    assert ready.get("/api/projects/demo").json()["stats"] is not None


def test_unknown_node_is_a_json_404(ready: TestClient, env: SimpleNamespace) -> None:
    initialized_repo(ready, env, "demo")
    response = ready.get("/api/projects/demo/node/evidence?qualified_name=pkg.nope")
    assert response.status_code == 404
    assert "not found" in response.json()["error"]


def test_two_projects_side_by_side_do_not_bleed(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.core import get_config
    from whygraph.core.context import current_project
    from whygraph.mcp.targets import repo_root

    root_a = initialized_repo(ready, env, "alpha")
    root_b = initialized_repo(ready, env, "beta")
    seen: list[tuple[str, Path, Path]] = []

    def _fake_run_turn(*, client, history, registry=None, **kwargs):
        # Runs inside the StreamingResponse generator, in the threadpool.
        ctx = current_project()
        seen.append((ctx.slug, repo_root(), get_config().whygraph_db))
        yield TextDelta(text=f"hello from {ctx.slug}")
        yield TurnDone("stop", 1, 2)

    monkeypatch.setattr(serve_chat, "run_turn", _fake_run_turn)
    monkeypatch.setattr(serve_chat, "make_chat_client", lambda *a, **k: object())

    for slug in ("alpha", "beta"):
        session = ready.post(f"/api/projects/{slug}/chat/sessions", json={}).json()
        stream = ready.post(
            f"/api/projects/{slug}/chat/sessions/{session['id']}/messages",
            json={"content": f"question for {slug}"},
        )
        assert stream.status_code == 200
        assert f"hello from {slug}" in stream.text

    assert seen == [
        ("alpha", root_a, root_a / ".whygraph" / "whygraph.db"),
        ("beta", root_b, root_b / ".whygraph" / "whygraph.db"),
    ]
    alpha = ready.get("/api/projects/alpha/chat/sessions").json()
    beta = ready.get("/api/projects/beta/chat/sessions").json()
    assert [s["title"] for s in alpha] == ["question for alpha"]
    assert [s["title"] for s in beta] == ["question for beta"]
    assert ready.get("/api/projects/alpha/tree").status_code == 200


# ---------------------------------------------------------------------------
# PATCH / DELETE
# ---------------------------------------------------------------------------


def test_patch_renames_the_name_only(ready: TestClient, env: SimpleNamespace) -> None:
    add_local(ready, make_repo(env.shared, "demo"))
    renamed = ready.patch("/api/projects/demo", json={"name": "Demo App"})
    assert renamed.status_code == 200
    assert (renamed.json()["slug"], renamed.json()["name"]) == ("demo", "Demo App")
    for body in ({"slug": "other"}, {"name": "x", "slug": "other"}, {"name": " "}):
        assert ready.patch("/api/projects/demo", json=body).status_code == 422
    assert ready.get("/api/projects/other").status_code == 404


def test_remove_strips_hooks_markers_and_only_whygraph_entries(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    (root / ".mcp.json").write_text('{"mcpServers": {"other": {"command": "x"}}}\n')
    add_local(ready, root)
    init_project(ready, "demo", agents=["claude"])
    assert ready.get("/api/projects/demo/chat/sessions").status_code == 200
    db_path = (root / ".whygraph" / "whygraph.db").resolve()
    assert db_path in db_engine._engines

    removed = ready.request(
        "DELETE", "/api/projects/demo", json={"strip_agent_entries": True}
    )
    assert removed.status_code == 200, removed.text
    assert db_path not in db_engine._engines
    assert managed_hook_names(root) == ()
    assert not (root / ".whygraph" / "portal.json").exists()
    assert not (root / ".whygraph" / "portal.env").exists()
    assert json.loads((root / ".mcp.json").read_text()) == {
        "mcpServers": {"other": {"command": "x"}}
    }
    # The repo and the rest of .whygraph/ stay.
    assert db_path.is_file() and (root / "sample.py").is_file()
    assert ready.get("/api/projects/demo").status_code == 404

    # Re-add: a fresh registration, not initialized, fresh context.
    again = add_local(ready, root)
    assert again["project"]["slug"] == "demo"
    assert again["detected"]["existing_db"] is True
    assert ready.get("/api/projects/demo/tree").status_code == 409
    init_project(ready, "demo")
    assert ready.get("/api/projects/demo/tree").status_code == 200


def test_remove_is_refused_while_a_scan_is_queued(
    ready: TestClient, env: SimpleNamespace
) -> None:
    add_local(ready, make_repo(env.shared, "demo"))
    with portal_db.get_session() as session:
        session.add(ScanRun(project_id=_project_id("demo"), trigger="manual"))
    assert ready.delete("/api/projects/demo").status_code == 409


def _fake_github(monkeypatch: pytest.MonkeyPatch, *, error: str | None = None) -> list:
    calls: list = []

    def _access(owner, name, token, **kw):
        calls.append(("probe", owner, name, token))
        if error:
            raise RepoAccessError(error, f"{error} for {owner}/{name}")
        return RepoAccess(
            full_name=f"{owner}/{name}", private=True, default_branch="main"
        )

    def _clone(url, dest, *, env=None, timeout=None):
        calls.append(("clone", url, dest, env))
        make_repo(dest.parent, dest.name)

    monkeypatch.setattr("whygraph.portal.routes.check_repo_access", _access)
    monkeypatch.setattr("whygraph.portal.routes.Repository.clone", _clone)
    return calls


def test_github_init_passes_no_hooks_and_a_hooks_change_installs_none(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plan section 4.4 / criterion 29: POST init on a GitHub clone passes
    # hooks=[] (Sync's fast-forward would fire post-merge in the container),
    # and a later [scan].hooks change does not install them either.
    from whygraph.portal import routes as portal_routes

    _fake_github(monkeypatch)
    added = ready.post(
        "/api/projects",
        json={"source": "github", "url": "https://github.com/acme/widget"},
    )
    assert added.status_code == 201, added.text
    dest = Path(added.json()["project"]["root"])
    seen: list = []
    real = portal_routes.initialize_project

    def _spy(root, **kwargs):
        seen.append(kwargs["hooks"])
        return real(root, **kwargs)

    monkeypatch.setattr(portal_routes, "initialize_project", _spy)
    assert init_project(ready, "widget")["initialized"] is True
    assert len(seen) == 1 and list(seen[0]) == []

    changed = ready.put(
        "/api/projects/widget/config",
        json={"config": {"scan": {"hooks": ["post-commit"]}}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["hooks"] is None
    assert managed_hook_names(dest) == ()
    assert not (dest / ".whygraph" / "hooks").exists()


def test_add_github_clones_under_the_data_dir(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_github(monkeypatch)
    token = "ghp_secret_token_7777"
    response = ready.post(
        "/api/projects",
        json={
            "source": "github",
            "url": "https://github.com/acme/widget.git",
            "token": token,
        },
    )
    assert response.status_code == 201, response.text
    assert token not in response.text
    project = response.json()["project"]
    dest = Path(os.path.realpath(env.data / "repos" / "widget"))
    assert project["slug"] == "widget" and project["root"] == str(dest)
    assert project["remote_url"] == "https://github.com/acme/widget"
    clone = next(c for c in calls if c[0] == "clone")
    assert clone[1] == "https://github.com/acme/widget"
    assert clone[3]["WHYGRAPH_GIT_TOKEN"] == token
    with portal_db.get_session() as session:
        assert session.exec(select(Project.root)).one() == "repos/widget"
    config = ready.get("/api/projects/widget/config").json()
    assert config["config"] == {"scan": {"forge": "auto"}}
    assert config["secrets"]["github_token"]["set"] is True

    # GitHub clones never get hooks (their Sync fast-forward would fire them).
    init = init_project(ready, "widget")
    assert init["hooks"]["installed"] == []
    assert managed_hook_names(dest) == ()

    duplicate = ready.post(
        "/api/projects",
        json={
            "source": "github",
            "url": "https://github.com/acme/widget",
            "token": token,
        },
    )
    assert duplicate.json()["code"] == "duplicate"

    refused = ready.delete("/api/projects/widget")
    assert refused.status_code == 409 and refused.json()["code"] == "confirm_name"
    assert dest.is_dir()
    removed = ready.request(
        "DELETE", "/api/projects/widget", json={"confirm_name": "widget"}
    )
    assert removed.status_code == 200
    assert removed.json()["checkout_deleted"] is True
    assert not dest.exists()


@pytest.mark.parametrize("code", ["bad_token", "no_access", "not_found"])
def test_add_github_maps_access_errors(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    _fake_github(monkeypatch, error=code)
    response = ready.post(
        "/api/projects",
        json={
            "source": "github",
            "url": "https://github.com/acme/widget",
            "token": "t",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == code


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc",
        "https://x:y@github.com/a/b",
        "ext::sh -c id",
        "https://gitlab.com/a/b",
    ],
)
def test_add_github_rejects_non_github_urls(ready: TestClient, url: str) -> None:
    response = ready.post(
        "/api/projects", json={"source": "github", "url": url, "token": "t"}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_url"


def test_remove_never_rmtrees_outside_the_repos_dir(
    ready: TestClient, env: SimpleNamespace
) -> None:
    outside = make_repo(env.tmp / "precious", "keep")
    with portal_db.get_session() as session:
        session.add(
            Project(slug="evil", name="evil", source="github", root=str(outside))
        )
    response = ready.request(
        "DELETE", "/api/projects/evil", json={"confirm_name": "evil"}
    )
    assert response.status_code == 200
    assert response.json()["checkout_deleted"] is False
    assert (outside / "sample.py").is_file()


def test_scan_endpoints_without_runs(ready: TestClient, env: SimpleNamespace) -> None:
    # Scanning itself is covered by tests/test_portal_runner.py.
    initialized_repo(ready, env, "demo")
    assert ready.get("/api/projects/demo/scans").json() == {"runs": []}
    assert ready.get("/api/projects/demo/scans/1/events").status_code == 404
    assert ready.post("/api/projects/demo/sync").json()["code"] == "not_github"


# ---------------------------------------------------------------------------
# Unit: shared folders and discovery
# ---------------------------------------------------------------------------


def test_parse_shared_folders_refuses_root_and_the_data_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    ok = tmp_path / "ok"
    ok.mkdir()
    value = ":".join(
        ["/", str(data), str(data / "inner"), str(tmp_path), "rel", str(ok), str(ok)]
    )
    assert portal_repos.parse_shared_folders(value, data) == (
        Path(os.path.realpath(ok)),
    )


def test_discovery_skips_links_and_noise_and_caps_depth(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    make_repo(shared, "top")
    make_repo(shared / "a" / "b" / "c", "deep")  # depth 4
    make_repo(shared / "a" / "b" / "c" / "d", "too-deep")  # depth 5
    make_repo(shared / "node_modules", "vendored")
    make_repo(shared / ".hidden", "dotted")
    outside = make_repo(tmp_path / "outside", "linked")
    (shared / "link").symlink_to(outside)
    found = {p.name for p in portal_repos.discover_repos((shared,))}
    assert found == {"top", "deep"}


def test_check_path_suggestion_is_never_root_or_home(tmp_path: Path) -> None:
    home_repo = Path.home() / "whygraph-test-nonexistent-repo"
    result = portal_repos.check_path(str(home_repo), (), tmp_path / "data")
    assert result["folder_suggestion"] == str(Path(os.path.realpath(home_repo)))
    result = portal_repos.check_path("/whygraph-nope", (), tmp_path / "data")
    assert result["folder_suggestion"] == "/whygraph-nope"
    spaced = portal_repos.check_path("/tmp/with space/repo", (), tmp_path / "data")
    assert spaced["command"].endswith("'" + os.path.realpath("/tmp/with space") + "'")


# ---------------------------------------------------------------------------
# CLI: whygraph portal
# ---------------------------------------------------------------------------


def _record_server_runs(monkeypatch: pytest.MonkeyPatch, runs: list[dict]) -> None:
    """Replace ``PortalServer.run`` with a recorder of the uvicorn config."""
    from whygraph.portal.app import PortalServer

    def _run(self: PortalServer) -> None:
        c = self.config
        runs.append(
            {
                "port": c.port,
                "host": c.host,
                "timeout_graceful_shutdown": c.timeout_graceful_shutdown,
                "workers": c.workers,
            }
        )

    monkeypatch.setattr(PortalServer, "run", _run)


def test_portal_cli_refuses_a_public_bind_outside_the_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from whygraph.cli import main

    runs: list[dict] = []
    _record_server_runs(monkeypatch, runs)
    monkeypatch.delenv("WHYGRAPH_IN_IMAGE", raising=False)
    monkeypatch.setenv("WHYGRAPH_DATA", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)

    refused = CliRunner().invoke(main, ["portal", "--host", "0.0.0.0"])
    assert refused.exit_code == 2
    assert "whygraph up" in refused.output
    assert runs == []

    ok = CliRunner().invoke(main, ["portal", "--host", "0.0.0.0", "--dev-expose"])
    assert ok.exit_code == 0, ok.output
    monkeypatch.setenv("WHYGRAPH_IN_IMAGE", "1")
    in_image = CliRunner().invoke(
        main, ["portal", "--host", "0.0.0.0", "--port", "9001"]
    )
    assert in_image.exit_code == 0, in_image.output
    assert len(runs) == 2
    for kw in runs:
        assert kw["timeout_graceful_shutdown"] == 10
        assert kw["workers"] == 1
    assert runs[1]["port"] == 9001


def test_portal_cli_refuses_a_data_dir_another_portal_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fcntl

    from whygraph.cli import main

    runs: list[dict] = []
    _record_server_runs(monkeypatch, runs)
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    # `--data` exports WHYGRAPH_DATA; setenv first so teardown restores it.
    monkeypatch.setenv("WHYGRAPH_DATA", str(data))
    with open(data / "portal.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = CliRunner().invoke(main, ["portal", "--data", str(data)])
    assert refused.exit_code == 2
    assert "already using" in refused.output
    assert runs == []
    ok = CliRunner().invoke(main, ["portal", "--data", str(data)])
    assert ok.exit_code == 0, ok.output
    assert len(runs) == 1


def test_claude_subscription_token_through_the_api(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the image the CLI has no login of its own, so the token is the key."""
    monkeypatch.setenv("WHYGRAPH_IN_IMAGE", "1")
    add_local(ready, make_repo(env.shared, "demo"))
    # Chat cannot use claude-cli (no tools), so it resolves to Anthropic.
    ready.put(
        "/api/portal/defaults", json={"secrets": {"llm": {"anthropic": "sk-a-0001"}}}
    )
    ready.put(
        "/api/projects/demo/config",
        json={"config": {"llm": {"model": "claude-cli/claude-opus-4-7"}}},
    )
    assert ready.get("/api/projects/demo").json()["missing_key"] == "claude-cli"

    saved = ready.put(
        "/api/portal/defaults",
        json={"secrets": {"claude_oauth_token": "sk-ant-oat01-abcd1234"}},
    )
    assert saved.status_code == 200
    assert saved.json()["secrets"]["claude_oauth_token"] == {
        "set": True,
        "hint": "…1234",
    }
    assert "sk-ant-oat01-abcd1234" not in saved.text
    assert ready.get("/api/projects/demo").json()["missing_key"] is None
    ctx = ready.app.state.portal.contexts.get(_project_id("demo"))
    assert ctx.config.llm.claude_cli.oauth_token == "sk-ant-oat01-abcd1234"

    ready.put("/api/portal/defaults", json={"secrets": {"claude_oauth_token": None}})
    assert ready.get("/api/projects/demo").json()["missing_key"] == "claude-cli"
    # Natively the CLI may use its own login: no token is not "missing".
    monkeypatch.delenv("WHYGRAPH_IN_IMAGE")
    assert ready.get("/api/projects/demo").json()["missing_key"] is None


def test_import_moves_a_claude_oauth_token_into_the_store(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "demo")
    (root / "whygraph.toml").write_text(
        '[llm]\nmodel = "claude-cli/claude-opus-4-7"\n'
        '[llm.claude_cli]\noauth_token = "sk-ant-oat01-imported77"\n'
    )
    response = ready.post("/api/projects", json={"source": "local", "path": str(root)})
    assert response.status_code == 201
    assert "sk-ant-oat01-imported77" not in response.text
    assert response.json()["import"]["secrets_moved"] == ["llm.claude_cli.oauth_token"]
    config = ready.get("/api/projects/demo/config").json()
    assert config["secrets"]["claude_oauth_token"] == {"set": True, "hint": "…ed77"}
    assert "claude_cli" not in config["config"].get("llm", {})
