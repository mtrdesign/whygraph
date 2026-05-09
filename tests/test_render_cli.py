"""Smoke for `run_render` (CLI entrypoint) end-to-end."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.render import run_render
from whygraph.scan import authors as authors_module
from whygraph.scan.db import Database
from whygraph.scan.git import Commit


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo_with_dbs(tmp_path: Path, fake_codegraph_db) -> Path:
    """Init a real git repo + WhyGraph DB + symlink the fake codegraph DB
    into the expected `.codegraph/codegraph.db` location."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    (pkg / "b.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    (pkg / "c.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    sha = _git_out(tmp_path, "rev-parse", "HEAD")

    wg_db = tmp_path / ".whygraph" / "whygraph.db"
    with Database(wg_db) as db:
        db.upsert_commit(
            Commit(
                sha=sha, parent_shas=[],
                author_name="Alice", author_email="alice@example.com",
                authored_at="2026-04-01T00:00:00+00:00",
                committed_at="2026-04-01T00:00:00+00:00",
                subject="init", body="",
                files_changed=3, insertions=15, deletions=0,
            )
        )
        authors_module.build_authors(db)

    cg_dir = tmp_path / ".codegraph"
    cg_dir.mkdir()
    (cg_dir / "codegraph.db").write_bytes(fake_codegraph_db.read_bytes())
    return tmp_path


def test_run_render_writes_html(repo_with_dbs: Path) -> None:
    out = repo_with_dbs / "out" / "viewer.html"
    rc = run_render(out_path=out, open_browser=False, repo_root=repo_with_dbs)
    assert rc == 0
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert 'id="whygraph-data"' in body
    assert '"runtime": "static"' in body
    assert "Cytoscape Consortium" in body


def test_run_render_default_path(repo_with_dbs: Path) -> None:
    rc = run_render(out_path=None, open_browser=False, repo_root=repo_with_dbs)
    assert rc == 0
    expected = repo_with_dbs / ".whygraph" / "whygraph.html"
    assert expected.exists()


def test_run_render_depth_caps_node_details(repo_with_dbs: Path) -> None:
    """Default depth=1 means non-module nodes have no detail block."""
    out_low = repo_with_dbs / "out" / "low.html"
    rc = run_render(
        out_path=out_low, open_browser=False, repo_root=repo_with_dbs, depth=1
    )
    assert rc == 0
    out_high = repo_with_dbs / "out" / "high.html"
    rc = run_render(
        out_path=out_high, open_browser=False, repo_root=repo_with_dbs, depth=4
    )
    assert rc == 0
    # depth=1 trims node_details for level 2+ kinds; depth=4 keeps everything.
    # Without re-parsing the JSON we can at least assert size shrinks.
    assert out_low.stat().st_size < out_high.stat().st_size
    # Both contain the meta.depth field reflecting the rendered cap.
    assert '"depth": 1' in out_low.read_text(encoding="utf-8")
    assert '"depth": 4' in out_high.read_text(encoding="utf-8")


def test_run_render_errors_when_codegraph_missing(tmp_path: Path) -> None:
    """Repo with no .codegraph/ directory should raise UsageError."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "x.txt").write_text("x")
    _git(tmp_path, "add", "x.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    import click
    with pytest.raises(click.UsageError, match="CodeGraph"):
        run_render(out_path=None, open_browser=False, repo_root=tmp_path)
