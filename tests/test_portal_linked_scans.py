"""A project linked to a platform scans CodeGraph only (M2e step 7, plan section 4.9).

A ``platform`` project is a local checkout whose evidence lives on the
platform: it has **no** local WhyGraph DB, its scans are
``whygraph scan --codegraph-only``, and its config context never reads the
org layer or any secret. Rows are inserted straight into the portal DB (the
link flow is a later step); the child scanner is ``fake_scan.py``.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `scanner`) imported from sibling tests

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import select

from conftest import builtin_org_id
from test_portal_app import (  # noqa: F401 -- `env` is a fixture
    _git,
    env,
    make_repo,
    seed_codegraph,
)
from test_portal_runner import (  # noqa: F401 -- `scanner` is a fixture
    FakeClock,
    client_for,
    commit,
    first_scan,
    runner_flags,
    runs,
    scanner,
    wait_idle,
    wait_run,
)
from whygraph import core
from whygraph.cli import main as whygraph_main
from whygraph.core.config import Config
from whygraph.hooks import managed_hook_names
from whygraph.portal import db as portal_db
from whygraph.portal.config_layers import save_layer
from whygraph.portal.context import build_project_context
from whygraph.portal.models import PlatformLink, Project, ScanRun, Secret
from whygraph.portal.runner import (
    ScanRunner,
    child_env,
    resolve_analyze,
    scan_argv,
    scan_flags,
)
from whygraph.portal.secrets import GITHUB_TOKEN, encrypt
from whygraph.services.codegraph import CodeGraphBootstrapError

CODEGRAPH_ONLY_FLAGS = ["--progress", "json", "--managed-by-portal", "--codegraph-only"]


def linked_row(root: Path, slug: str, *, initialized: bool = False) -> int:
    """Insert a ``platform`` project (and its link row) over ``root``; its id."""
    with portal_db.get_session() as session:
        project = Project(
            org_id=builtin_org_id(session),
            slug=slug,
            name=slug,
            source="platform",
            root=str(root),
            remote_url=f"https://github.com/acme/{slug}",
            initialized_at="2026-10-01T00:00:00+00:00" if initialized else None,
        )
        session.add(project)
        session.flush()
        session.add(
            PlatformLink(
                project_id=project.id,
                platform_origin="https://whygraph.example",
                api_origin="https://acme.whygraph.example",
                org_slug="acme",
                remote_slug=slug,
                remote_name=slug,
                clone_url=f"https://github.com/acme/{slug}.git",
                default_branch="main",
                token_ciphertext=encrypt("wgc_secret"),
                token_hint="...cret",
            )
        )
        return project.id


def linked_project(client: TestClient, env: SimpleNamespace, slug: str) -> Path:
    """A linked, initialized project over a fresh repo (a CodeGraph index, no DB)."""
    root = make_repo(env.shared, slug)
    seed_codegraph(root)
    linked_row(root, slug)
    response = client.post(f"/api/projects/{slug}/init", json={"agents": []})
    assert response.status_code == 200, response.text
    assert response.json()["initialized"] is True
    return root


def whygraph_db(root: Path) -> Path:
    return root / ".whygraph" / "whygraph.db"


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    monkeypatch.chdir(root)
    monkeypatch.setattr("whygraph.cli.configure_logging", lambda *a, **kw: None)
    monkeypatch.setattr(core, "_config", Config())
    return root


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_codegraph_only_requires_managed_by_portal(repo: Path) -> None:
    result = CliRunner().invoke(whygraph_main, ["scan", "--codegraph-only"])
    assert result.exit_code == 2
    assert "--codegraph-only requires --managed-by-portal" in result.output
    help_out = CliRunner().invoke(whygraph_main, ["scan", "--help"]).output
    assert "--codegraph-only" not in help_out


def test_codegraph_only_scan_creates_no_whygraph_db(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        "whygraph.scan.codegraph_crawler.refresh_codegraph_index",
        lambda root, **kw: calls.append(root),
    )
    result = CliRunner().invoke(
        whygraph_main,
        ["scan", "--progress", "json", "--managed-by-portal", "--codegraph-only"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [repo]
    events = _events(result.stdout)
    assert events[0] == {"type": "start", "phase_total": 1}
    assert events[-1]["type"] == "result" and events[-1]["status"] == "ok"
    assert [c["name"] for c in events[-1]["crawlers"]] == ["codegraph"]
    assert (repo / ".whygraph" / "scan.log").is_file()
    assert not whygraph_db(repo).exists()


def test_codegraph_only_scan_fails_on_codegraph_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(root: Path, **kw: object) -> None:
        raise CodeGraphBootstrapError("codegraph exited with status 3")

    monkeypatch.setattr("whygraph.scan.codegraph_crawler.refresh_codegraph_index", boom)
    result = CliRunner().invoke(
        whygraph_main,
        ["scan", "--progress", "json", "--managed-by-portal", "--codegraph-only"],
    )
    assert result.exit_code == 1, result.output
    last = _events(result.stdout)[-1]
    assert last["type"] == "result" and last["status"] == "failed"
    assert last["crawlers"][0]["status"] == "failed"
    assert "status 3" in last["crawlers"][0]["error"]
    assert "status 3" in result.stderr
    assert not whygraph_db(repo).exists()


# ---------------------------------------------------------------------------
# The runner's argv, env and refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger", ["initial", "hook", "poll", "sync", "manual", "describe"]
)
def test_platform_flags_are_always_codegraph_only(trigger: str) -> None:
    assert resolve_analyze(trigger, None, "platform") is False
    assert resolve_analyze(trigger, True, "platform") is False
    assert scan_flags(trigger, False, "platform") == ["--codegraph-only"]
    argv = scan_argv(trigger, False, {"WHYGRAPH_SCAN_CMD": "whygraph scan"}, "platform")
    assert argv == ["whygraph", "scan", *CODEGRAPH_ONLY_FLAGS]


def test_linked_manual_scan_is_codegraph_only(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        root = linked_project(client, env, "lnk")
        first = first_scan(client, "lnk")
        assert first["analyze"] is False
        manual = wait_run(client, "lnk", _scan(client, "lnk"))
        assert (manual["trigger"], manual["analyze"]) == ("manual", False)
        explicit_off = wait_run(client, "lnk", _scan(client, "lnk", analyze=False))
        assert explicit_off["status"] == "ok"
        hook = wait_run(client, "lnk", _scan(client, "lnk", trigger="hook"))
        assert hook["trigger"] == "hook"
    calls = scanner.calls()
    assert len(calls) == 4
    assert all(runner_flags(c) == CODEGRAPH_ONLY_FLAGS for c in calls)
    assert all(c["cwd"] == str(root) for c in calls)


def _scan(client: TestClient, slug: str, **body: object) -> int:
    response = client.post(f"/api/projects/{slug}/scans", json=body or None)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_linked_explicit_analyze_and_describe_refused(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        linked_project(client, env, "lnk")
        first_scan(client, "lnk")
        for body in ({"analyze": True}, {"trigger": "describe"}):
            refused = client.post("/api/projects/lnk/scans", json=body)
            assert refused.status_code == 403, refused.text
            assert refused.json()["code"] == "managed_on_platform"
        assert len(runs(client, "lnk")) == 1  # nothing queued
    assert len(scanner.calls()) == 1


def test_linked_child_env_has_no_secrets(tmp_path: Path) -> None:
    portal_env = {
        "PATH": "/bin",
        "HOME": "/home/me",
        "ANTHROPIC_API_KEY": "sk-portal-env",
        "GH_TOKEN": "gh-portal-env",
    }
    config = Config.from_dict(
        {
            "llm": {
                "model": "anthropic/claude-opus-4-7",
                "anthropic": {"api_key": "sk-proj"},
            },
            "scan": {"forge": "auto", "token": "ghp_project"},
        },
        tmp_path,
    )
    env, secrets = child_env(
        config, {"scan": {}}, source="platform", analyze=False, environ=portal_env
    )
    assert secrets == []
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert name not in env
    assert "WHYGRAPH_GITHUB_TOKEN_FILE" not in env
    # The developer's git config stays in effect (only a server clone ignores it).
    assert "GIT_CONFIG_GLOBAL" not in env
    assert "GIT_CONFIG_NOSYSTEM" not in env


def test_linked_context_reads_only_the_scan_table_and_no_secrets(
    env: SimpleNamespace,
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        root = make_repo(env.shared, "lnk")
        linked_id = linked_row(root, "lnk")
        local_root = make_repo(env.shared, "loc")
        with portal_db.get_session() as session:
            org_id = builtin_org_id(session)
            local = Project(
                org_id=org_id,
                slug="loc",
                name="loc",
                source="local",
                root=str(local_root),
            )
            session.add(local)
            session.flush()
            save_layer(session, None, {"analyze": {"max_workers": 3}}, org_id=org_id)
            for pid in (linked_id, local.id):
                save_layer(
                    session,
                    pid,
                    {"scan": {"hooks": ["post-commit"]}, "analyze": {"max_workers": 5}},
                    org_id=org_id,
                )
            session.add(
                Secret(
                    org_id=org_id,
                    project_id=None,
                    kind=GITHUB_TOKEN,
                    provider=None,
                    ciphertext=encrypt("ghp_org_default"),
                    hint="...ault",
                )
            )
            session.flush()
            linked_cfg = build_project_context(
                session, session.get(Project, linked_id)
            ).config
            local_cfg = build_project_context(session, local).config
    assert tuple(linked_cfg.scan_hooks) == ("post-commit",)
    assert linked_cfg.scan_token is None
    assert linked_cfg.analyze.max_workers == Config().analyze.max_workers
    # The same rows on a local project do apply (the control).
    assert local_cfg.scan_token == "ghp_org_default"
    assert local_cfg.analyze.max_workers == 5


# ---------------------------------------------------------------------------
# No local WhyGraph DB
# ---------------------------------------------------------------------------


def test_linked_project_never_creates_whygraph_db(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        root = make_repo(env.shared, "lnk")
        seed_codegraph(root)
        linked_row(root, "lnk")
        assert (
            client.post("/api/projects/lnk/init", json={"agents": []}).status_code
            == 200
        )
        first_scan(client, "lnk")
        detail = client.get("/api/projects/lnk")
        assert detail.status_code == 200, detail.text
        assert detail.json()["stats"] is None
        assert client.get("/api/projects").status_code == 200
        assert client.get("/api/projects/lnk/config").status_code == 200
        assert client.get("/api/projects/lnk/scans").status_code == 200
    assert not whygraph_db(root).exists()


def test_relink_of_a_former_local_checkout_ignores_its_db(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "old")
    seed_codegraph(root)
    leftover = whygraph_db(root)
    leftover.parent.mkdir()
    leftover.write_bytes(b"not a sqlite database, and never opened")
    before = leftover.read_bytes()
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        linked_row(root, "old")
        init = client.post("/api/projects/old/init", json={"agents": []})
        assert init.status_code == 200, init.text
        assert init.json()["initialized"] is True
        assert init.json()["custom_db_paths"] == []
        first_scan(client, "old")
        assert client.get("/api/projects/old").json()["stats"] is None
    assert leftover.read_bytes() == before


# ---------------------------------------------------------------------------
# Hooks, catch-up, add
# ---------------------------------------------------------------------------


def test_linked_hooks_toggle_resyncs(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        root = linked_project(client, env, "lnk")
        assert set(managed_hook_names(root)) == {
            "post-commit",
            "post-merge",
            "post-rewrite",
            "post-checkout",
        }
        response = client.put(
            "/api/projects/lnk/config",
            json={"config": {"scan": {"hooks": ["post-commit"]}}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["hooks"]["installed"] == ["post-commit"]
        assert managed_hook_names(root) == ("post-commit",)


def test_catch_up_includes_linked(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        root = linked_project(client, env, "lnk")
        first_scan(client, "lnk")
    commit(root, "committed while the portal was down\n")
    with client_for(ScanRunner(sleep=FakeClock().sleep)) as client:
        rs = wait_idle(client, "lnk")
        assert len(rs) == 2 and (rs[0]["trigger"], rs[0]["status"]) == ("hook", "ok")
    assert runner_flags(scanner.calls()[-1]) == CODEGRAPH_ONLY_FLAGS
    with portal_db.get_session() as session:
        assert len(session.exec(select(ScanRun.id)).all()) == 2


def test_platform_source_still_refused_on_add(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "lnk")
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        response = client.post(
            "/api/projects",
            json={"source": "platform", "path": str(root), "link_id": "x"},
        )
        assert response.status_code == 422, response.text
        assert client.get("/api/projects").json()["projects"] == []
