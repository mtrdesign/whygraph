"""The ``plans_dir=`` line of ``.whygraph/portal.env`` (plan 61, R14, section 4.16)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_portal_app import (  # noqa: F401 -- fixtures
    client,
    env,
    initialized_repo,
    ready,
)

from whygraph.project_setup import (
    PortalMarker,
    read_portal_marker,
    sync_plans_marker,
    write_portal_marker,
)

ENV = Path(".whygraph") / "portal.env"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _env(root: Path) -> str:
    return (root / ENV).read_text()


def test_write_with_plans_dir_adds_the_third_line(root: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765, plans_dir="docs/plans"))
    assert _env(root) == "slug=demo\nport=8765\nplans_dir=docs/plans\n"


def test_empty_plans_dir_writes_an_empty_line(root: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765, plans_dir=""))
    assert _env(root) == "slug=demo\nport=8765\nplans_dir=\n"


def test_none_keeps_a_valid_existing_line(root: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765, plans_dir="docs/plans"))
    write_portal_marker(root, PortalMarker("demo", 9100))
    assert _env(root) == "slug=demo\nport=9100\nplans_dir=docs/plans\n"
    write_portal_marker(root, PortalMarker("demo", 9100, plans_dir=""))
    write_portal_marker(root, PortalMarker("demo", 9101))
    assert _env(root) == "slug=demo\nport=9101\nplans_dir=\n"


@pytest.mark.parametrize("bad", ["../x", ".claude/x", "/abs"])
def test_none_drops_an_invalid_existing_line(root: Path, bad: str) -> None:
    (root / ENV).parent.mkdir()
    (root / ENV).write_text(f"slug=demo\nport=8765\nplans_dir={bad}\n")
    write_portal_marker(root, PortalMarker("demo", 8765))
    assert _env(root) == "slug=demo\nport=8765\n"


def test_none_without_a_line_writes_two_lines(root: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765))
    assert _env(root) == "slug=demo\nport=8765\n"


@pytest.mark.parametrize("bad", ["../x", ".claude/x", "a\nb", "/abs"])
def test_marker_refuses_an_invalid_plans_dir(bad: str) -> None:
    with pytest.raises(ValueError):
        PortalMarker("demo", 8765, plans_dir=bad)


def test_read_portal_marker_still_returns_slug_and_port_only(root: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765, plans_dir="docs/plans"))
    marker, warning = read_portal_marker(root)
    assert warning is None
    assert marker == PortalMarker("demo", 8765)


def test_sync_writes_then_is_a_noop_when_it_matches(root: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765))
    assert sync_plans_marker(root, "plans") is None
    assert _env(root) == "slug=demo\nport=8765\nplans_dir=plans\n"
    path = root / ENV
    before = path.stat().st_ino
    assert sync_plans_marker(root, "plans") is None
    assert path.stat().st_ino == before  # not rewritten (os.replace changes inode)
    assert sync_plans_marker(root, "") is None
    assert _env(root) == "slug=demo\nport=8765\nplans_dir=\n"


def test_sync_none_is_a_noop(root: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765, plans_dir="plans"))
    assert sync_plans_marker(root, None) is None
    assert _env(root).endswith("plans_dir=plans\n")


def test_sync_refuses_a_symlinked_env(root: Path, tmp_path: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765))
    outside = tmp_path / "x.env"
    outside.write_text("slug=demo\nport=8765\n")
    (root / ENV).unlink()
    (root / ENV).symlink_to(outside)
    err = sync_plans_marker(root, "plans")
    assert isinstance(err, str) and err
    assert outside.read_text() == "slug=demo\nport=8765\n"


def test_sync_refuses_a_tracked_env(root: Path) -> None:
    write_portal_marker(root, PortalMarker("demo", 8765))
    subprocess.run(["git", "add", "-f", str(ENV)], cwd=root, check=True)
    err = sync_plans_marker(root, "plans")
    assert isinstance(err, str) and "tracked" in err
    assert "plans_dir" not in _env(root)


def test_sync_without_a_marker_or_with_a_bad_value_returns_a_string(
    root: Path,
) -> None:
    assert isinstance(sync_plans_marker(root, "plans"), str)
    write_portal_marker(root, PortalMarker("demo", 8765))
    assert isinstance(sync_plans_marker(root, "../x"), str)
    assert "plans_dir" not in _env(root)


# --- the portal half (section 9.2 case 13a, local) ---------------------------


def test_initialize_writes_the_configured_plans_dir(ready, env) -> None:  # noqa: F811
    root = initialized_repo(ready, env, "demo")
    lines = _env(root).splitlines()
    assert lines[0] == "slug=demo" and lines[2] == "plans_dir=docs/plans"


def test_a_plans_dir_save_rewrites_the_line_and_reports_no_error(
    ready,  # noqa: F811
    env,  # noqa: F811
) -> None:
    root = initialized_repo(ready, env, "demo")
    response = ready.put(
        "/api/projects/demo/config", json={"config": {"plans": {"dir": "notes/plans"}}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["plans_marker_error"] is None
    assert _env(root).endswith("plans_dir=notes/plans\n")
