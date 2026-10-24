"""A project's history in the portal's data directory (project-db-in-data-dir plan).

The path seam (:mod:`whygraph.portal.paths`): where a project's WhyGraph DB
lives, and the check that refuses a symlink inside the data directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from whygraph.core.safe_paths import UnsafePathError
from whygraph.portal import db as portal_db
from whygraph.portal.paths import (
    check_project_db,
    project_data_dir,
    project_db_path,
)


def test_project_db_path_is_under_projects_org_slug() -> None:
    data = portal_db.data_dir()
    assert project_data_dir(data, "acme", "api") == data / "projects" / "acme" / "api"
    assert project_db_path(data, "acme", "api") == (
        data / "projects" / "acme" / "api" / "whygraph.db"
    )


def test_project_db_path_refuses_a_relative_data_dir() -> None:
    # The scan child's cwd is the repository: a relative path would land in it.
    with pytest.raises(AssertionError):
        project_db_path(Path("data"), "local", "api")


def test_data_dir_is_absolute_for_a_relative_whygraph_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(portal_db.DATA_ENV_VAR, "rel/data")
    data = portal_db.data_dir()
    assert data.is_absolute()
    assert data == Path(os.path.abspath(tmp_path / "rel" / "data"))
    assert data.is_dir()


def test_data_dir_keeps_a_symlinked_mounts_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # abspath, not resolve(): check_inside compares lexically, so the spelling
    # the paths were built from must be the one they are checked against.
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "mount").symlink_to(real)
    monkeypatch.setenv(portal_db.DATA_ENV_VAR, str(tmp_path / "mount"))
    assert portal_db.data_dir() == tmp_path / "mount"


def test_check_project_db_accepts_a_plain_or_missing_path() -> None:
    data = portal_db.data_dir()
    db = project_db_path(data, "local", "api")
    assert check_project_db(data, db) == db  # nothing exists yet
    db.parent.mkdir(parents=True)
    db.touch()
    assert check_project_db(data, db) == db


@pytest.mark.parametrize("link", ["slug_dir", "org_dir", "db"])
def test_check_project_db_refuses_a_symlink_inside_the_data_dir(
    tmp_path: Path, link: str
) -> None:
    data = portal_db.data_dir()
    db = project_db_path(data, "local", "api")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if link == "slug_dir":
        db.parent.parent.mkdir(parents=True)
        db.parent.symlink_to(elsewhere)
    elif link == "org_dir":
        (data / "projects").mkdir()
        db.parent.parent.symlink_to(elsewhere)
    else:
        db.parent.mkdir(parents=True)
        (elsewhere / "victim.db").touch()
        db.symlink_to(elsewhere / "victim.db")
    with pytest.raises(UnsafePathError):
        check_project_db(data, db)


def test_check_project_db_refuses_a_path_outside_the_data_dir(tmp_path: Path) -> None:
    data = portal_db.data_dir()
    with pytest.raises(UnsafePathError):
        check_project_db(data, tmp_path / "repo" / ".whygraph" / "whygraph.db")
