"""A portal port change follows into the managed repos (plan sections 4.8, 4.13).

The portal is started on 8765, projects are initialized (markers, agent
files), then it is restarted on another port: mounted markers are
rewritten, an untracked literal entry (Codex, Cursor) is rewritten, an
env-interpolated or tracked file is never touched but reported, and
unmounted roots are listed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from test_portal_app import (  # noqa: F401 -- `env` is a fixture
    add_local,
    env,
    init_project,
    make_repo,
    portal_client,
    seed_codegraph,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _initialized(client, env: SimpleNamespace, name: str, agents: list[str]) -> Path:  # noqa: F811
    root = make_repo(env.shared, name)
    seed_codegraph(root)
    slug = add_local(client, root)["project"]["slug"]
    assert init_project(client, slug, agents=agents)["initialized"] is True
    return root


def test_a_port_change_rewrites_markers_and_literal_entries_only(
    env: SimpleNamespace,  # noqa: F811
) -> None:
    with portal_client(8765) as client:
        assert (
            client.post("/api/portal/setup", json={"display_name": "T"}).status_code
            == 201
        )
        alpha = _initialized(client, env, "alpha", ["claude", "codex", "cursor"])
        beta = _initialized(client, env, "beta", ["claude"])
        # nothing to report on a first start at the recorded port
        assert client.get("/api/portal/state").json()["port_change"] is None

    # A teammate committed .mcp.json and .cursor/mcp.json; codex stays untracked.
    _git(alpha, "add", "-f", ".mcp.json", ".cursor/mcp.json")
    _git(alpha, "commit", "-q", "-m", "agent configs")
    claude_before = (alpha / ".mcp.json").read_bytes()
    cursor_before = (alpha / ".cursor" / "mcp.json").read_bytes()
    # beta's folder is no longer shared / mounted.
    beta.rename(beta.with_name("beta-moved"))

    with portal_client(9100) as client:
        report = client.get("/api/portal/state").json()["port_change"]
        details = client.get("/api/projects/alpha").json()["port_change"]
        beta_details = client.get("/api/projects/beta").json()["port_change"]

    # Markers of the mounted project are rewritten (both files).
    assert json.loads((alpha / ".whygraph" / "portal.json").read_text()) == {
        "slug": "alpha",
        "port": 9100,
    }
    assert (alpha / ".whygraph" / "portal.env").read_text() == "slug=alpha\nport=9100\n"

    # The untracked Codex literal is rewritten (only the whygraph key's URL).
    codex = (alpha / ".codex" / "config.toml").read_text()
    assert 'url = "http://127.0.0.1:9100/mcp/alpha"' in codex

    # Tracked files are byte-identical; the env-interpolated one needs no rewrite.
    assert (alpha / ".mcp.json").read_bytes() == claude_before
    assert (alpha / ".cursor" / "mcp.json").read_bytes() == cursor_before

    assert report["port"] == 9100 and report["previous_port"] == 8765
    (item,) = report["projects"]
    assert item["slug"] == "alpha" and item["markers"] == "rewritten"
    assert item["previous_port"] == 8765
    by_agent = {a["agent"]: a for a in item["agents"]}
    assert by_agent["codex"]["action"] == "rewritten"
    assert by_agent["claude"]["action"] == "env"
    assert "WHYGRAPH_PORT=9100" in by_agent["claude"]["hint"]
    cursor = by_agent["cursor"]
    assert cursor["action"] == "manual"
    assert cursor["line"] == '"url": "http://127.0.0.1:9100/mcp/alpha"'
    assert "9100" in cursor["diff"]
    assert details == item

    # The unmounted root is listed (it could not be updated).
    assert report["unmounted"] == [{"slug": "beta", "root": str(beta)}]
    assert beta_details["unmounted"] is True and beta_details["port"] == 9100

    # A later start on the same port has nothing to report.
    with portal_client(9100) as client:
        assert client.get("/api/portal/state").json()["port_change"] is None


def test_a_symlinked_marker_is_left_alone(env: SimpleNamespace, tmp_path: Path) -> None:  # noqa: F811
    with portal_client(8765) as client:
        client.post("/api/portal/setup", json={"display_name": "T"})
        root = _initialized(client, env, "gamma", [])
    target = tmp_path / "outside.env"
    target.write_text("slug=gamma\nport=8765\n")
    marker_env = root / ".whygraph" / "portal.env"
    marker_env.unlink()
    marker_env.symlink_to(target)

    with portal_client(9200) as client:
        report = client.get("/api/portal/state").json()["port_change"]

    (item,) = report["projects"]
    assert item["markers"] == "skipped" and "symbolic link" in item["reason"]
    assert target.read_text() == "slug=gamma\nport=8765\n"
