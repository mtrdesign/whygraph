"""A project's history in the portal's data directory (project-db-in-data-dir plan).

The path seam (:mod:`whygraph.portal.paths`): where a project's WhyGraph DB
lives, and the check that refuses a symlink inside the data directory. The
identity sidecar (:mod:`whygraph.portal.history`): an exclusive reservation
of a history directory, and the slug choice that re-attaches a repository's
own kept history and never another's. Removal: the guarded delete of a
history directory, by mode and on request.
"""

from __future__ import annotations

# ruff: noqa: F811 -- pytest fixtures (`env`, `ready`, `client`) imported from test_portal_app

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from conftest import builtin_org_id
from test_portal_app import (  # noqa: F401 -- fixtures
    add_local,
    client,
    env,
    history_db,
    init_project,
    make_repo,
    ready,
    seed_codegraph,
    seed_history,
)

from whygraph.core.safe_paths import UnsafePathError
from whygraph.db import engine as db_engine
from whygraph.portal import db as portal_db
from whygraph.portal.clones import portal_dir_is_safe
from whygraph.portal.history import (
    SIDECAR,
    find_history,
    foreign_history,
    history_owner,
    release_sidecar,
    remove_history,
    write_sidecar,
)
from whygraph.portal.models import Project
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


# ---------------------------------------------------------------------------
# The identity sidecar (plan section 4.4, D22)
# ---------------------------------------------------------------------------

ORIGIN_A = {"origin": "github.com/acme/api"}
ORIGIN_B = {"origin": "github.com/other/api"}


def _write_raw_sidecar(slug_dir: Path, **fields: object) -> None:
    """A sidecar with chosen fields (``created_at`` / ``root`` for the sort key)."""
    body = {
        "version": 1,
        "org": slug_dir.parent.name,
        "slug": slug_dir.name,
        "source": "local",
        "identity": ORIGIN_A,
        "root": None,
        "created_at": "2026-10-01T00:00:00Z",
        **fields,
    }
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / SIDECAR).write_text(json.dumps(body))


def test_write_sidecar_reserves_a_fresh_slug_and_reads_back() -> None:
    data = portal_db.data_dir()
    first = write_sidecar(data, "local", "api", "local", ORIGIN_A, "/src/api")
    assert first and first.created_sidecar and first.created_dir
    owner = history_owner(project_data_dir(data, "local", "api"))
    assert owner is not None
    assert {k: owner[k] for k in ("version", "org", "slug", "source", "root")} == {
        "version": 1,
        "org": "local",
        "slug": "api",
        "source": "local",
        "root": "/src/api",
    }
    assert owner["identity"] == ORIGIN_A
    # No temporary file is left behind.
    assert sorted(p.name for p in first.slug_dir.iterdir()) == [SIDECAR]


def test_write_sidecar_is_exclusive() -> None:
    data = portal_db.data_dir()
    assert write_sidecar(data, "local", "api", "local", ORIGIN_A, "/src/a")
    before = (project_data_dir(data, "local", "api") / SIDECAR).read_bytes()

    # The same identity re-attaches; it created nothing.
    again = write_sidecar(data, "local", "api", "local", ORIGIN_A, "/src/a2")
    assert again and not again.created_sidecar and not again.created_dir
    # Another identity is refused, and the file is never overwritten.
    other = write_sidecar(data, "local", "api", "local", ORIGIN_B, "/src/b")
    assert not other and not other.created_sidecar
    assert (project_data_dir(data, "local", "api") / SIDECAR).read_bytes() == before


def test_write_sidecar_refuses_an_unreadable_existing_sidecar() -> None:
    data = portal_db.data_dir()
    slug_dir = project_data_dir(data, "local", "api")
    slug_dir.mkdir(parents=True)
    (slug_dir / SIDECAR).write_text("{not json")
    assert not write_sidecar(data, "local", "api", "local", ORIGIN_A, None)
    assert (slug_dir / SIDECAR).read_text() == "{not json"


@pytest.mark.parametrize("link", ["slug_dir", "org_dir", "projects"])
def test_write_sidecar_never_writes_through_a_symlinked_directory(
    tmp_path: Path, link: str
) -> None:
    data = portal_db.data_dir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    slug_dir = project_data_dir(data, "local", "api")
    target = {"slug_dir": slug_dir, "org_dir": slug_dir.parent}.get(
        link, data / "projects"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(elsewhere)
    with pytest.raises(UnsafePathError):
        write_sidecar(data, "local", "api", "local", ORIGIN_A, None)
    assert list(elsewhere.iterdir()) == []


def test_write_sidecar_never_writes_through_a_symlinked_sidecar(
    tmp_path: Path,
) -> None:
    data = portal_db.data_dir()
    victim = tmp_path / "victim.json"
    victim.write_text("victim")
    slug_dir = project_data_dir(data, "local", "api")
    slug_dir.mkdir(parents=True)
    (slug_dir / SIDECAR).symlink_to(victim)
    with pytest.raises(UnsafePathError):
        write_sidecar(data, "local", "api", "local", ORIGIN_A, None)
    assert victim.read_text() == "victim"


def test_a_symlinked_sidecar_reads_as_foreign(tmp_path: Path) -> None:
    data = portal_db.data_dir()
    real = tmp_path / "real.json"
    _write_raw_sidecar(tmp_path / "local" / "api")
    real.write_bytes((tmp_path / "local" / "api" / SIDECAR).read_bytes())
    slug_dir = project_data_dir(data, "local", "api")
    slug_dir.mkdir(parents=True)
    (slug_dir / SIDECAR).symlink_to(real)
    assert history_owner(slug_dir) is None
    assert find_history(data, "local", ORIGIN_A, None, set()) is None
    assert foreign_history(data, "local", ORIGIN_A) == {"api"}


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[]",
        "{not json",
        json.dumps({"version": 2, "slug": "api"}),
        # Two identity keys, or an unknown one.
        json.dumps(
            {
                "version": 1,
                "org": "local",
                "slug": "api",
                "source": "local",
                "identity": {**ORIGIN_A, "path": "/x"},
                "root": None,
                "created_at": "2026-10-01T00:00:00Z",
            }
        ),
        json.dumps(
            {
                "version": 1,
                "org": "local",
                "slug": "api",
                "source": "local",
                "identity": {"name": "api"},
                "root": None,
                "created_at": "2026-10-01T00:00:00Z",
            }
        ),
    ],
)
def test_a_malformed_sidecar_is_foreign(content: str) -> None:
    data = portal_db.data_dir()
    slug_dir = project_data_dir(data, "local", "api")
    slug_dir.mkdir(parents=True)
    (slug_dir / SIDECAR).write_text(content)
    assert history_owner(slug_dir) is None
    assert find_history(data, "local", ORIGIN_A, None, set()) is None
    assert foreign_history(data, "local", ORIGIN_A) == {"api"}


def test_find_history_matches_only_its_own_identity() -> None:
    data = portal_db.data_dir()
    _write_raw_sidecar(project_data_dir(data, "local", "api"))
    _write_raw_sidecar(project_data_dir(data, "local", "web"), identity=ORIGIN_B)
    project_data_dir(data, "local", "bare").mkdir()  # no sidecar: foreign

    assert find_history(data, "local", ORIGIN_A, None, set()) == "api"
    assert find_history(data, "local", ORIGIN_B, None, set()) == "web"
    assert find_history(data, "local", {"path": "/x"}, None, set()) is None
    assert foreign_history(data, "local", ORIGIN_A) == {"web", "bare"}
    # Another org's directory never matches.
    assert find_history(data, "acme", ORIGIN_A, None, set()) is None
    assert foreign_history(data, "acme", ORIGIN_A) == set()


def test_find_history_skips_used_slugs_and_mismatched_names() -> None:
    data = portal_db.data_dir()
    _write_raw_sidecar(project_data_dir(data, "local", "api"))
    assert find_history(data, "local", ORIGIN_A, None, {"api"}) is None
    # A directory whose name is not its sidecar's slug (a copy, a rename).
    _write_raw_sidecar(project_data_dir(data, "local", "copy"), slug="api")
    assert find_history(data, "local", ORIGIN_A, None, {"api"}) is None
    assert "copy" in foreign_history(data, "local", ORIGIN_A)
    # A sidecar naming another org is not this org's.
    _write_raw_sidecar(project_data_dir(data, "local", "moved"), org="acme")
    assert find_history(data, "local", ORIGIN_A, None, {"api"}) is None
    # A name that is not a valid slug is never a candidate.
    _write_raw_sidecar(project_data_dir(data, "local", "Bad_Name"), slug="Bad_Name")
    assert find_history(data, "local", ORIGIN_A, None, {"api"}) is None


def test_find_history_skips_a_symlinked_directory(tmp_path: Path) -> None:
    data = portal_db.data_dir()
    real = tmp_path / "real" / "local" / "api"
    _write_raw_sidecar(real)
    org_dir = project_data_dir(data, "local", "api").parent
    org_dir.mkdir(parents=True)
    (org_dir / "api").symlink_to(real)
    assert find_history(data, "local", ORIGIN_A, None, set()) is None
    assert foreign_history(data, "local", ORIGIN_A) == {"api"}
    # A symlinked org directory hides everything below it.
    (data / "projects" / "other").symlink_to(tmp_path / "real" / "local")
    assert find_history(data, "other", ORIGIN_A, None, set()) is None


def test_find_history_sort_key() -> None:
    """``(root == root) desc, has whygraph.db desc, created_at asc, slug asc``."""
    data = portal_db.data_dir()

    def history(slug: str, *, root: str | None, created: str, db: bool) -> None:
        slug_dir = project_data_dir(data, "local", slug)
        _write_raw_sidecar(slug_dir, root=root, created_at=created)
        if db:
            (slug_dir / "whygraph.db").write_bytes(b"")

    history("b", root="/src/x", created="2026-10-03T00:00:00Z", db=False)
    history("a", root="/src/x", created="2026-10-03T00:00:00Z", db=False)
    history("old", root="/src/x", created="2026-10-02T00:00:00Z", db=False)
    history("withdb", root="/src/x", created="2026-10-09T00:00:00Z", db=True)
    history("here", root="/src/here", created="2026-10-09T00:00:00Z", db=False)

    def pick(root: str | None, used: set[str] = set()) -> str | None:  # noqa: B006
        return find_history(data, "local", ORIGIN_A, root, used)

    assert pick("/src/here") == "here"  # the same checkout first
    assert pick("/src/x") == "withdb"  # then one with a DB
    assert pick(None) == "withdb"  # production: no root tie-break
    assert pick("/src/x", {"withdb"}) == "old"  # then the oldest
    assert pick("/src/x", {"withdb", "old"}) == "a"  # then by slug
    assert pick("/src/x", {"withdb", "old", "a", "b", "here"}) is None


def test_release_sidecar_removes_only_what_it_created() -> None:
    data = portal_db.data_dir()
    fresh = write_sidecar(data, "local", "fresh", "local", ORIGIN_A, None)
    assert release_sidecar(fresh, data_dir=data) is True
    assert not fresh.slug_dir.exists()
    assert fresh.slug_dir.parent.is_dir()

    # A re-attach created nothing: nothing is released.
    seeded = write_sidecar(data, "local", "kept", "local", ORIGIN_A, None)
    again = write_sidecar(data, "local", "kept", "local", ORIGIN_A, None)
    assert release_sidecar(again, data_dir=data) is False
    assert (seeded.slug_dir / SIDECAR).is_file()

    # A directory that holds a DB keeps everything.
    withdb = write_sidecar(data, "local", "withdb", "local", ORIGIN_A, None)
    (withdb.slug_dir / "whygraph.db").write_bytes(b"")
    assert release_sidecar(withdb, data_dir=data) is False
    assert (withdb.slug_dir / SIDECAR).is_file()

    # A directory that existed keeps its other content.
    other = project_data_dir(data, "local", "other")
    other.mkdir()
    (other / "backups").mkdir()
    taken = write_sidecar(data, "local", "other", "local", ORIGIN_A, None)
    assert taken.created_sidecar and not taken.created_dir
    assert release_sidecar(taken, data_dir=data) is False
    assert sorted(p.name for p in other.iterdir()) == ["backups"]


# ---------------------------------------------------------------------------
# Slug choice on a local add (plan section 4.4, criteria 3 and 4)
# ---------------------------------------------------------------------------


def _owner(slug: str, org: str = "local") -> dict | None:
    return history_owner(project_data_dir(portal_db.data_dir(), org, slug))


def _remove(client: TestClient, slug: str) -> None:
    response = client.request("DELETE", f"/api/projects/{slug}", json={})
    assert response.status_code == 200, response.text


def test_a_readd_from_a_renamed_folder_reattaches_by_origin(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "api", remote="https://github.com/acme/api.git")
    seed_codegraph(root)
    added = add_local(ready, root)
    assert added["project"]["slug"] == "api"
    assert added["detected"]["existing_db"] is False
    identity = _owner("api")["identity"]
    assert set(identity) == {"origin"} and identity["origin"].endswith("/acme/api")
    init_project(ready, "api")
    assert history_db("api").is_file()
    _remove(ready, "api")

    moved = root.rename(env.shared / "api-renamed")
    again = add_local(ready, moved)
    assert again["project"]["slug"] == "api"  # not "api-renamed"
    assert again["project"]["name"] == "api-renamed"
    assert again["detected"]["existing_db"] is True
    assert _owner("api")["identity"] == identity
    assert not project_data_dir(portal_db.data_dir(), "local", "api-renamed").exists()


def test_a_path_identity_does_not_survive_a_rename(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "api")  # no origin: identified by its path (D11)
    seed_codegraph(root)
    add_local(ready, root)
    assert _owner("api")["identity"] == {"path": str(root)}
    init_project(ready, "api")
    _remove(ready, "api")

    moved = root.rename(env.shared / "moved")
    again = add_local(ready, moved, name="api")
    assert again["project"]["slug"] == "api-2"
    assert again["detected"]["existing_db"] is False
    assert _owner("api-2")["identity"] == {"path": str(moved)}
    # The old history stays, still its own.
    assert history_db("api").is_file()
    assert _owner("api")["identity"] == {"path": str(root)}


def test_another_repositorys_history_gets_a_suffix(
    ready: TestClient, env: SimpleNamespace
) -> None:
    kept = seed_history(None, "api", {"origin": "github.com/other/api"})
    root = make_repo(env.shared, "api")
    added = add_local(ready, root)
    assert added["project"]["slug"] == "api-2"
    assert added["detected"]["existing_db"] is False
    assert _owner("api")["identity"] == {"origin": "github.com/other/api"}
    assert _owner("api-2")["identity"] == {"path": str(root)}
    assert kept.is_file()


def test_a_directory_without_a_sidecar_is_never_attached(
    ready: TestClient, env: SimpleNamespace
) -> None:
    stray = project_data_dir(portal_db.data_dir(), "local", "api")
    stray.mkdir(parents=True)
    (stray / "whygraph.db").write_bytes(b"")
    root = make_repo(env.shared, "api")
    assert add_local(ready, root)["project"]["slug"] == "api-2"
    assert sorted(p.name for p in stray.iterdir()) == ["whygraph.db"]


def test_interleaved_adds_of_two_repos_with_one_name_keep_their_own_history(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D22: B chose ``api`` too, but A reserved it first; B moves on to ``api-2``."""
    from whygraph.portal import routes

    root_a = make_repo(env.shared / "a", "api")
    root_b = make_repo(env.shared / "b", "api")
    identity_a = {"path": str(root_a)}
    real = routes.write_sidecar
    raced: list[str] = []

    def racing(data, org_slug, slug, source, identity, root):  # noqa: ANN001, ANN202
        if identity != identity_a and not raced:
            # A's add reserves the same slug between B's choice and B's write.
            raced.append(slug)
            assert real(data, org_slug, slug, "local", identity_a, str(root_a))
        return real(data, org_slug, slug, source, identity, root)

    monkeypatch.setattr(routes, "write_sidecar", racing)
    b = add_local(ready, root_b)
    assert raced == ["api"]
    assert b["project"]["slug"] == "api-2"
    # A's own add (which had reserved "api") goes on to insert it.
    a = add_local(ready, root_a)
    assert a["project"]["slug"] == "api"
    assert _owner("api")["identity"] == identity_a
    assert _owner("api-2")["identity"] == {"path": str(root_b)}


def test_a_failed_insert_removes_only_the_directory_it_created(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.portal import routes

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("the insert failed")

    monkeypatch.setattr(routes, "_insert_project", boom)
    fresh = make_repo(env.shared, "fresh")
    with pytest.raises(RuntimeError):
        ready.post("/api/projects", json={"source": "local", "path": str(fresh)})
    assert not project_data_dir(portal_db.data_dir(), "local", "fresh").exists()

    # A kept history it re-attached is left exactly as it was.
    kept = make_repo(env.shared, "kept")
    db = seed_history(kept, "kept")
    sidecar = (db.parent / SIDECAR).read_bytes()
    with pytest.raises(RuntimeError):
        ready.post("/api/projects", json={"source": "local", "path": str(kept)})
    assert db.is_file() and (db.parent / SIDECAR).read_bytes() == sidecar
    with portal_db.get_session() as session:
        assert session.exec(select(Project)).all() == []


def test_a_slug_a_concurrent_link_committed_first_is_released_and_retried(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uq_projects_org_slug``: the reservation goes, the add takes the next slug."""
    from whygraph.portal import routes

    real = routes.write_sidecar
    linked: list[str] = []

    def then_a_link(data, org_slug, slug, source, identity, root):  # noqa: ANN001, ANN202
        reservation = real(data, org_slug, slug, source, identity, root)
        if not linked:
            linked.append(slug)
            with portal_db.get_session() as session:
                session.add(
                    Project(
                        org_id=builtin_org_id(session),
                        slug=slug,
                        name=slug,
                        source="platform",
                        root=str(env.tmp / "linked-checkout"),
                    )
                )
        return reservation

    monkeypatch.setattr(routes, "write_sidecar", then_a_link)
    root = make_repo(env.shared, "api")
    added = add_local(ready, root)
    assert linked == ["api"]
    assert added["project"]["slug"] == "api-2"
    # The linked row has no history: the reservation under its slug is gone.
    assert not project_data_dir(portal_db.data_dir(), "local", "api").exists()
    assert _owner("api-2")["identity"] == {"path": str(root)}


# ---------------------------------------------------------------------------
# The rmtree guard and remove_history (plan section 4.6)
# ---------------------------------------------------------------------------


def test_the_generalized_guard_at_projects(tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "projects" / "acme" / "api").mkdir(parents=True)
    (data / "repos" / "acme" / "api").mkdir(parents=True)
    projects = data / "projects"
    assert portal_dir_is_safe(projects / "acme" / "api", data, base="projects", depth=2)
    assert portal_dir_is_safe(projects / "acme", data, base="projects", depth=1)
    assert not portal_dir_is_safe(projects / "acme", data, base="projects", depth=2)
    assert not portal_dir_is_safe(
        projects / "acme" / "api", data, base="projects", depth=1
    )
    # One base never vouches for the other.
    assert not portal_dir_is_safe(
        data / "repos" / "acme" / "api", data, base="projects", depth=2
    )
    assert not portal_dir_is_safe(
        projects / "acme" / "api", data, base="repos", depth=2
    )
    assert not portal_dir_is_safe(
        tmp_path / "elsewhere" / "x" / "y", data, base="projects", depth=2
    )
    for base, depth in (("runs", 1), ("", 1), ("projects", 0)):
        with pytest.raises(ValueError):
            portal_dir_is_safe(projects / "acme", data, base=base, depth=depth)


def test_remove_history_deletes_a_history_directory() -> None:
    db = seed_history(None, "api", {"origin": "github.com/acme/api"})
    slug_dir = db.parent
    assert remove_history(slug_dir) is True
    assert not slug_dir.exists()
    assert slug_dir.parent.is_dir()  # the org's folder stays
    assert remove_history(slug_dir) is False  # already gone


def test_remove_history_refuses_a_symlink(tmp_path: Path) -> None:
    data = portal_db.data_dir()
    victim = tmp_path / "victim"
    (victim / "inner").mkdir(parents=True)
    (victim / "inner" / "keep.txt").write_text("keep")
    org_dir = data / "projects" / "local"
    org_dir.mkdir(parents=True)
    # The history directory itself is a symlink.
    (org_dir / "api").symlink_to(victim / "inner")
    assert remove_history(org_dir / "api") is False
    # A symlinked org folder in between.
    (data / "projects" / "evil").symlink_to(victim)
    assert remove_history(data / "projects" / "evil" / "inner") is False
    assert (victim / "inner" / "keep.txt").read_text() == "keep"


def test_remove_history_refuses_a_wrong_depth_or_a_path_outside(
    tmp_path: Path,
) -> None:
    data = portal_db.data_dir()
    org_dir = data / "projects" / "local"
    (org_dir / "api" / "nested").mkdir(parents=True)
    assert remove_history(org_dir) is False  # depth 1
    assert remove_history(data / "projects") is False  # depth 0
    assert remove_history(org_dir / "api" / "nested") is False  # depth 3
    repos_dir = data / "repos" / "local" / "api"
    repos_dir.mkdir(parents=True)
    assert remove_history(repos_dir) is False  # right depth, not under projects/
    outside = tmp_path / "x" / "y" / "z"
    outside.mkdir(parents=True)
    assert remove_history(outside) is False
    assert (org_dir / "api" / "nested").is_dir()
    assert repos_dir.is_dir() and outside.is_dir()


# ---------------------------------------------------------------------------
# Remove by mode and on request (plan section 4.6, criteria 3 and 5)
# ---------------------------------------------------------------------------


def test_a_local_remove_keeps_history_unless_asked(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "api")
    seed_codegraph(root)
    add_local(ready, root)
    init_project(ready, "api")
    db = history_db("api")
    assert db.is_file()

    # Omitted: a local remove keeps the history (D9).
    kept = ready.request("DELETE", "/api/projects/api", json={})
    assert kept.status_code == 200, kept.text
    assert kept.json()["history_deleted"] is False
    assert kept.json()["warnings"] == []
    assert db.is_file()


def test_a_local_history_delete_needs_the_project_name(
    ready: TestClient, env: SimpleNamespace
) -> None:
    root = make_repo(env.shared, "api")
    seed_codegraph(root)
    add_local(ready, root)
    assert ready.patch("/api/projects/api", json={"name": "My API"}).status_code == 200
    init_project(ready, "api")
    db = history_db("api")
    hooks_before = sorted(p.name for p in (root / ".git" / "hooks").iterdir())

    for body in (
        {"delete_history": True},
        {"delete_history": True, "confirm_name": "api"},  # the slug is not the name
    ):
        refused = ready.request("DELETE", "/api/projects/api", json=body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "confirm_name"
        assert refused.json()["error"] == (
            "type the project name to delete its history"
        )
        assert "folder" not in refused.json()
    # Nothing changed.
    assert ready.get("/api/projects/api").status_code == 200
    assert ready.get("/api/projects/api/tree").status_code == 200
    assert db.is_file()
    assert (root / ".whygraph" / "portal.json").is_file()
    assert sorted(p.name for p in (root / ".git" / "hooks").iterdir()) == hooks_before

    removed = ready.request(
        "DELETE",
        "/api/projects/api",
        json={"delete_history": True, "confirm_name": "My API"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["history_deleted"] is True
    assert removed.json()["warnings"] == []
    assert not db.parent.exists()
    assert (root / "sample.py").is_file()  # the repository itself stays


def test_a_history_delete_needs_no_checkout(
    ready: TestClient, env: SimpleNamespace
) -> None:
    """No identity is recomputed from the checkout (D14): a deleted one still works."""
    root = make_repo(env.shared, "api", remote="https://github.com/acme/api.git")
    seed_codegraph(root)
    add_local(ready, root)
    init_project(ready, "api")
    db = history_db("api")
    shutil.rmtree(root)

    removed = ready.request(
        "DELETE",
        "/api/projects/api",
        json={"delete_history": True, "confirm_name": "api"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["history_deleted"] is True
    assert not db.parent.exists()


def test_a_readd_after_a_history_delete_creates_the_db_again(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request in flight during the remove cannot leave a stale engine or memo."""
    from whygraph.portal import routes

    state = ready.app.state.portal
    root = make_repo(env.shared, "api")
    seed_codegraph(root)
    add_local(ready, root)
    init_project(ready, "api")
    db = history_db("api")
    key = db.resolve()
    real = routes.remove_history

    def in_flight(slug_dir: Path) -> bool:
        # Between the first dispose / forget and the rmtree, a request still
        # running opens the DB and records its migration.
        with db_engine._engines_lock:
            db_engine._engines[key] = db_engine._build_engine(db)
        state.migrations._done.add(key)
        return real(slug_dir)

    monkeypatch.setattr(routes, "remove_history", in_flight)
    removed = ready.request(
        "DELETE",
        "/api/projects/api",
        json={"delete_history": True, "confirm_name": "api"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["history_deleted"] is True
    assert key not in db_engine._engines
    assert key not in state.migrations._done
    assert not db.parent.exists()

    again = add_local(ready, root)
    assert again["project"]["slug"] == "api"
    assert again["detected"]["existing_db"] is False
    init_project(ready, "api")
    assert db.is_file()
    assert ready.get("/api/projects/api/tree").status_code == 200
