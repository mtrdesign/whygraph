"""Regression tests for the first portal security review (security fix pass 1).

One section per finding:

1. a symlinked project DB (or ``.whygraph/`` / ``.codegraph/``) must never
   let one project open another project's DB or the portal's own;
2. an imported or stored ``[scan].remote`` must never become a git option;
3. init / import / delete never read or write through a symlink out of the
   repository root;
4. two concurrent GitHub adds of one URL never delete each other's clone;
5. a slow first principal load never overwrites the principal setup set;
6. a project-scope key never follows an inherited global endpoint change.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `ready`, `client`) imported from test_portal_app

from __future__ import annotations

import os
import subprocess
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    add_local,
    client,
    env,
    initialized_repo,
    make_repo,
    manual_ctx,
    ready,
    seed_codegraph,
)
from whygraph.core.config import Config, ConfigError
from whygraph.core.safe_paths import UnsafePathError, check_inside
from whygraph.hooks import helper_path
from whygraph.portal import db as portal_db
from whygraph.portal import deps as portal_deps
from whygraph.portal.deps import PortalState
from whygraph.portal.migrate import ProjectMigrations
from whygraph.portal.models import Project, Secret
from whygraph.portal.runner import ScanRunner
from whygraph.portal.security import Principal
from whygraph.project_setup import PORTAL_ENV, PORTAL_JSON
from whygraph.services.git import GitError, Repository
from whygraph.services.git.commands import (
    GitFetchDefaultCmd,
    GitFetchRefsCmd,
    GitRemoteUrlCmd,
)
from whygraph.services.git.credentials import GITHUB_GIT_CONFIG, TOKEN_ENV_VAR
from whygraph.services.github import RepoAccess

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
MCP_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _mark_initialized(slug: str) -> None:
    with portal_db.get_session() as session:
        row = session.exec(select(Project).where(Project.slug == slug)).one()
        row.initialized_at = "2026-01-01T00:00:00+00:00"
        session.add(row)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_check_inside(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "dir").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert check_inside(root, "dir/new.txt") == root / "dir" / "new.txt"
    assert check_inside(root, root / "missing" / "x") == root / "missing" / "x"
    (root / "link").symlink_to(outside)
    (root / "dir" / "file").symlink_to(root / "dir")  # even an inside symlink
    for rel in ("link", "link/x", "dir/file", "../outside", str(outside)):
        with pytest.raises(UnsafePathError):
            check_inside(root, rel)
    # The root itself may be reached through a symlink.
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    assert check_inside(alias, "dir/new.txt") == alias / "dir" / "new.txt"


# ---------------------------------------------------------------------------
# 1. Symlinked project DB (MAJOR)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("link", ["db", "dir"])
def test_symlinked_db_to_other_project_is_refused(
    ready: TestClient, env: SimpleNamespace, link: str
) -> None:
    alpha = initialized_repo(ready, env, "alpha")
    assert ready.post("/api/projects/alpha/chat/sessions", json={}).status_code == 201
    beta = make_repo(env.shared, "beta")
    seed_codegraph(beta)
    if link == "db":
        (beta / ".whygraph").mkdir()
        (beta / ".whygraph" / "whygraph.db").symlink_to(
            alpha / ".whygraph" / "whygraph.db"
        )
    else:
        (beta / ".whygraph").symlink_to(alpha / ".whygraph")
    add_local(ready, beta)

    response = ready.post("/api/projects/beta/init", json={"agents": []})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "unsafe_path"

    # Even when the row claims it is initialized, nothing opens alpha's DB.
    _mark_initialized("beta")
    for path in ("/api/projects/beta/chat/sessions", "/api/projects/beta/tree"):
        response = ready.get(path)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "unsafe_path"
        assert "id" not in response.json()  # no session list, no tree
    mcp = ready.post("/mcp/beta", json=MCP_LIST, headers=MCP_HEADERS)
    assert mcp.status_code == 409 and mcp.json()["code"] == "unsafe_path"
    assert ready.post("/api/projects/beta/scans").json()["code"] == "unsafe_path"
    details = ready.get("/api/projects/beta")
    assert details.status_code == 200 and details.json()["stats"] is None


def test_symlinked_codegraph_db_is_refused(
    ready: TestClient, env: SimpleNamespace
) -> None:
    alpha = initialized_repo(ready, env, "alpha")
    beta = make_repo(env.shared, "beta")
    (beta / ".codegraph").mkdir()
    (beta / ".codegraph" / "codegraph.db").symlink_to(
        alpha / ".codegraph" / "codegraph.db"
    )
    add_local(ready, beta)
    response = ready.post("/api/projects/beta/init", json={"agents": []})
    assert response.status_code == 409 and response.json()["code"] == "unsafe_path"
    _mark_initialized("beta")
    (beta / ".whygraph").mkdir()
    sqlite3.connect(beta / ".whygraph" / "whygraph.db").close()
    assert ready.get("/api/projects/beta/tree").json()["code"] == "unsafe_path"


def test_symlinked_db_never_backs_up_or_migrates_a_data_dir_file(
    ready: TestClient, env: SimpleNamespace
) -> None:
    # The portal's own DB is Postgres; what a symlink can still reach is any
    # SQLite file inside the data directory.
    portal_file = env.data / "victim.db"
    conn = sqlite3.connect(portal_file)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    before = _tables(portal_file)
    before_bytes = portal_file.read_bytes()
    root = make_repo(env.shared, "sneaky")
    (root / ".whygraph").mkdir()
    (root / ".whygraph" / "whygraph.db").symlink_to(
        os.path.relpath(portal_file, root / ".whygraph")
    )
    add_local(ready, root)
    response = ready.post("/api/projects/sneaky/init", json={"agents": []})
    assert response.status_code == 409 and response.json()["code"] == "unsafe_path"
    assert not (root / ".whygraph" / "backups").exists()
    assert _tables(portal_file) == before
    assert portal_file.read_bytes() == before_bytes


def test_migrations_refuse_a_symlinked_db(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".whygraph").mkdir(parents=True)
    victim = tmp_path / "victim.db"
    sqlite3.connect(victim).close()
    (root / ".whygraph" / "whygraph.db").symlink_to(victim)
    with pytest.raises(UnsafePathError):
        ProjectMigrations().ensure(manual_ctx(root))
    assert not (root / ".whygraph" / "backups").exists()
    assert _tables(victim) == set()


# ---------------------------------------------------------------------------
# 2. [scan].remote as a git option (MAJOR)
# ---------------------------------------------------------------------------

EVIL_REMOTE = "--upload-pack=touch pwned;"


@pytest.mark.parametrize(
    "scan",
    [
        {"remote": EVIL_REMOTE},
        {"remote": "origin; rm -rf /"},
        {"remote": "-o"},
        {"default_branch": "--output=/tmp/y"},
    ],
)
def test_config_rejects_option_like_remote_and_branch(
    tmp_path: Path, scan: dict
) -> None:
    with pytest.raises(ConfigError):
        Config.from_dict({"scan": scan}, tmp_path)


def test_config_accepts_ordinary_remote_and_branch(tmp_path: Path) -> None:
    config = Config.from_dict(
        {"scan": {"remote": "up-stream_2/x.y", "default_branch": "release/1.x"}},
        tmp_path,
    )
    assert config.scan_remote == "up-stream_2/x.y"
    assert config.scan_default_branch == "release/1.x"


def test_git_commands_end_options_before_the_remote() -> None:
    assert GitFetchRefsCmd("a:b", remote="up").argv()[-3:] == ["--", "up", "a:b"]
    assert GitFetchDefaultCmd("up").argv()[-2:] == ["--", "up"]
    assert GitRemoteUrlCmd("up").argv()[-2:] == ["--", "up"]


def test_pr_ref_fetch_uses_the_token_helper_only_with_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix pass 2: a private clone's PR refs were fetched without credentials."""
    token = "ghp_fetchrefs_secret_1234"
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    plain = GitFetchRefsCmd("refs/pull/1/head:refs/whygraph/pull/1").argv()
    assert plain == [
        "git",
        "fetch",
        "--no-tags",
        "--",
        "origin",
        "refs/pull/1/head:refs/whygraph/pull/1",
    ]

    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    argv = GitFetchRefsCmd("refs/pull/1/head:refs/whygraph/pull/1").argv()
    assert argv[: 1 + len(GITHUB_GIT_CONFIG)] == ["git", *GITHUB_GIT_CONFIG]
    assert argv[-3:] == ["--", "origin", "refs/pull/1/head:refs/whygraph/pull/1"]
    assert all(token not in a for a in argv)

    # The argv's helper answers github.com over https with the env token...
    config = list(GITHUB_GIT_CONFIG)
    fill = subprocess.run(
        ["git", *config, "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=acme/private\n\n",
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        cwd=tmp_path,
    )
    assert f"password={token}" in fill.stdout
    # ...and a real (refused, non-https) fetch never persists it.
    root = make_repo(tmp_path, "repo")
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "elsewhere")],
        cwd=root,
        check=True,
    )
    with pytest.raises(GitError):  # file transport: refused by protocol.allow
        Repository(root).fetch_refs(["refs/heads/main:refs/whygraph/x"])
    assert token not in (root / ".git" / "config").read_text()


def test_option_like_remote_never_executes(tmp_path: Path) -> None:
    root = make_repo(tmp_path, "repo")
    marker = tmp_path / "pwned"
    repo = Repository(root, origin_remote=f"--upload-pack=touch {marker};")
    with pytest.raises(GitError):
        repo.fetch_refs(["refs/heads/main:refs/whygraph/x"])
    assert repo.origin_url is None
    assert not marker.exists()


def test_import_drops_option_like_remote(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "evil")
    (root / "whygraph.toml").write_text(
        f'[scan]\nremote = "{EVIL_REMOTE}"\ndefault_branch = "--output=/tmp/y"\n'
        'forge = "off"\n'
    )
    body = add_local(ready, root)
    dropped = {d["key"] for d in body["import"]["dropped"]}
    assert {"scan.remote", "scan.default_branch"} <= dropped
    config = ready.get("/api/projects/evil/config").json()["config"]
    assert config == {"scan": {"forge": "off"}}
    assert ready.get("/api/projects/evil").status_code == 200


def test_put_config_rejects_option_like_remote(
    ready: TestClient, env: SimpleNamespace
) -> None:
    add_local(ready, make_repo(env.shared, "demo"))
    response = ready.put(
        "/api/projects/demo/config", json={"config": {"scan": {"remote": "-x"}}}
    )
    assert response.status_code == 422
    assert ready.get("/api/projects/demo/config").json()["config"] == {}


# ---------------------------------------------------------------------------
# 3. init / import / delete through symlinks (MINOR)
# ---------------------------------------------------------------------------


def test_init_refuses_a_symlinked_gitignore(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "gi")
    victim = env.tmp / "victim.txt"
    victim.write_text("precious\n")
    (root / ".gitignore").symlink_to(victim)
    add_local(ready, root)
    for dry_run in (True, False):
        response = ready.post(
            "/api/projects/gi/init", json={"agents": [], "dry_run": dry_run}
        )
        assert response.status_code == 409 and response.json()["code"] == "unsafe_path"
    assert victim.read_text() == "precious\n"


def test_init_refuses_a_gitignore_linked_to_the_secret_key(
    ready: TestClient, env: SimpleNamespace
) -> None:
    ready.put(
        "/api/portal/defaults",
        json={"secrets": {"llm": {"anthropic": "sk-ant-12345678"}}},
    )
    key_file = env.data / "secret.key"
    key = key_file.read_bytes()
    root = make_repo(env.shared, "gk")
    (root / ".gitignore").symlink_to(os.path.relpath(key_file, root))
    add_local(ready, root)
    response = ready.post("/api/projects/gk/init", json={"agents": []})
    assert response.status_code == 409 and response.json()["code"] == "unsafe_path"
    assert key_file.read_bytes() == key


def test_init_refuses_a_symlinked_marker(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "mk")
    victim = env.tmp / "victim.txt"
    victim.write_text("precious\n")
    (root / ".whygraph").mkdir()
    (root / PORTAL_JSON).symlink_to(victim)
    add_local(ready, root)
    response = ready.post("/api/projects/mk/init", json={"agents": []})
    assert response.status_code == 409 and response.json()["code"] == "unsafe_path"
    assert victim.read_text() == "precious\n"
    assert not (root / PORTAL_ENV).exists()


@pytest.mark.parametrize("link", [".mcp.json", ".claude"])
def test_init_refuses_symlinked_agent_files(
    ready: TestClient, env: SimpleNamespace, link: str
) -> None:
    root = make_repo(env.shared, "ag")
    outside = env.tmp / "outside"
    outside.mkdir()
    victim = outside / "victim.json"
    victim.write_text("{}\n")
    (root / link).symlink_to(victim if link == ".mcp.json" else outside)
    add_local(ready, root)
    response = ready.post("/api/projects/ag/init", json={"agents": ["claude"]})
    assert response.status_code == 409 and response.json()["code"] == "unsafe_path"
    assert victim.read_text() == "{}\n"
    assert sorted(p.name for p in outside.iterdir()) == ["victim.json"]


def test_import_refuses_a_symlinked_whygraph_toml(
    ready: TestClient, env: SimpleNamespace
) -> None:
    sibling = make_repo(env.shared, "sibling")
    (sibling / "whygraph.toml").write_text(
        '[llm.anthropic]\napi_key = "sk-ant-sibling-secret"\n'
        "[analyze]\nmax_workers = 3\n"
    )
    root = make_repo(env.shared, "thief")
    (root / "whygraph.toml").symlink_to(sibling / "whygraph.toml")
    body = add_local(ready, root)
    assert body["import"]["found"] is True
    assert "symbolic link" in body["import"]["error"]
    config = ready.get("/api/projects/thief/config").json()
    assert config["config"] == {}
    assert config["secrets"]["llm"]["anthropic"]["set"] is False
    with portal_db.get_session() as session:
        assert session.exec(select(Secret)).all() == []


def test_delete_never_unlinks_through_a_symlinked_whygraph_dir(
    ready: TestClient, env: SimpleNamespace
) -> None:
    alpha = initialized_repo(ready, env, "alpha")
    assert (alpha / PORTAL_JSON).is_file()
    helper = helper_path(alpha)
    assert helper.is_file()
    beta = make_repo(env.shared, "beta")
    (beta / ".whygraph").symlink_to(alpha / ".whygraph")
    add_local(ready, beta)
    response = ready.delete("/api/projects/beta")
    assert response.status_code == 200, response.text
    assert any("symbolic link" in w for w in response.json()["warnings"])
    assert (alpha / PORTAL_JSON).is_file() and (alpha / PORTAL_ENV).is_file()
    assert helper.is_file()


# ---------------------------------------------------------------------------
# 4. Concurrent GitHub adds (MINOR)
# ---------------------------------------------------------------------------


def test_concurrent_github_adds_keep_the_winners_clone(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    both_cloning = threading.Barrier(2, timeout=1.0)
    clones: list[Path] = []

    def _access(owner, name, token, **kw):
        return RepoAccess(
            full_name=f"{owner}/{name}", private=False, default_branch="main"
        )

    def _clone(url, dest, *, env=None, timeout=None):
        clones.append(dest)
        try:
            both_cloning.wait()  # before the fix both requests get here
        except threading.BrokenBarrierError:
            pass
        if dest.exists():
            raise GitError(f"destination path '{dest}' already exists")
        make_repo(dest.parent, dest.name)
        (dest / "WINNER").write_text("mine\n")

    monkeypatch.setattr("whygraph.portal.routes.check_repo_access", _access)
    monkeypatch.setattr("whygraph.portal.routes.Repository.clone", _clone)

    results: list = []

    def add() -> None:
        results.append(
            ready.post(
                "/api/projects",
                json={"source": "github", "url": "https://github.com/acme/widget"},
            )
        )

    threads = [threading.Thread(target=add) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["code"] == "duplicate"
    dest = env.data / "repos" / "widget"
    assert (dest / "WINNER").read_text() == "mine\n"
    # Only the final checkout is left under repos/.
    assert sorted(p.name for p in (env.data / "repos").iterdir()) == ["widget"]


def test_failed_clone_removes_only_its_own_directory(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _access(owner, name, token, **kw):
        return RepoAccess(
            full_name=f"{owner}/{name}", private=False, default_branch="main"
        )

    def _clone(url, dest, *, env=None, timeout=None):
        dest.mkdir(parents=True)
        (dest / "partial").write_text("x")
        raise GitError("network down")

    monkeypatch.setattr("whygraph.portal.routes.check_repo_access", _access)
    monkeypatch.setattr("whygraph.portal.routes.Repository.clone", _clone)
    response = ready.post(
        "/api/projects", json={"source": "github", "url": "https://github.com/acme/w"}
    )
    assert response.status_code == 502 and response.json()["code"] == "clone_failed"
    assert list((env.data / "repos").iterdir()) == []


# ---------------------------------------------------------------------------
# 5. Principal cache race (MINOR)
# ---------------------------------------------------------------------------


def test_slow_principal_load_never_overwrites_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()

    def slow_load():
        started.set()
        release.wait(5)
        return None  # what the DB said before setup ran

    monkeypatch.setattr(portal_deps, "_load_local_principal", slow_load)
    state = PortalState(port=8765, data_dir=tmp_path, runner=ScanRunner())
    tess = Principal(user_id=1, uid="u1", display_name="Tess")
    seen: list = []

    async def main() -> None:
        async def resolve() -> None:
            seen.append(await state.resolve_principal({}))

        async with anyio.create_task_group() as tg:
            tg.start_soon(resolve)
            await anyio.to_thread.run_sync(started.wait)
            state.set_principal(tess)  # POST /setup while the load is in flight
            release.set()

    anyio.run(main)
    assert seen == [tess]
    assert anyio.run(state.resolve_principal, {}) is tess


# ---------------------------------------------------------------------------
# 6. Project key and an inherited global endpoint change (MINOR)
# ---------------------------------------------------------------------------


def test_global_endpoint_change_clears_inheriting_project_keys(
    ready: TestClient, env: SimpleNamespace
) -> None:
    add_local(ready, make_repo(env.shared, "inherits"))
    add_local(ready, make_repo(env.shared, "overrides"))
    assert (
        ready.put(
            "/api/projects/inherits/config",
            json={"secrets": {"llm": {"openai": "sk-inherits-1234"}}},
        ).status_code
        == 200
    )
    assert (
        ready.put(
            "/api/projects/overrides/config",
            json={
                "config": {"llm": {"openai": {"base_url": "https://own.example/v1"}}},
                "secrets": {"llm": {"openai": "sk-overrides-5678"}},
            },
        ).status_code
        == 200
    )

    response = ready.put(
        "/api/portal/defaults",
        json={"config": {"llm": {"openai": {"base_url": "https://new.example/v1"}}}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["cleared_project_keys"] == [
        {"slug": "inherits", "provider": "openai"}
    ]
    inherits = ready.get("/api/projects/inherits/config").json()["secrets"]["llm"]
    overrides = ready.get("/api/projects/overrides/config").json()["secrets"]["llm"]
    assert inherits["openai"]["set"] is False
    assert overrides["openai"]["set"] is True

    # A save that leaves the endpoint alone clears nothing.
    ready.put(
        "/api/projects/inherits/config",
        json={"secrets": {"llm": {"openai": "sk-inherits-9999"}}},
    )
    again = ready.put(
        "/api/portal/defaults",
        json={
            "config": {
                "llm": {"openai": {"base_url": "https://new.example/v1"}},
                "chat": {"max_tool_rounds": 3},
            }
        },
    )
    assert again.json()["cleared_project_keys"] == []
    inherits = ready.get("/api/projects/inherits/config").json()["secrets"]["llm"]
    assert inherits["openai"]["set"] is True
