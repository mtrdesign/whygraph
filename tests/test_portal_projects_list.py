"""The projects list payload, its stale cache and the small S10 fixes (M2f-3 plan section 4.10).

- ``GET /api/projects`` runs a constant number of statements whatever the
  number of projects (PRJ-4);
- ``last_scan_stats`` comes from the newest ``summary.coverage`` snapshot and
  a non-JSON ``summary`` never fails the list; ``importing``;
- ``stale`` goes through :class:`~whygraph.portal.runner.StaleCache`;
- ``check_path`` reports ``exists`` and an add answers ``400 path_missing``
  (BUG-9); discovery searches while walking (BUG-20).

Production's ``root: null`` (MODE-1) is pinned by the GitHub import tests.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `client`, `ready`) imported from test_portal_app
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import select

from conftest import builtin_org_id
from test_portal_app import (  # noqa: F401 -- fixtures
    _git,
    add_local,
    client,
    env,
    initialized_repo,
    make_repo,
    ready,
)
from whygraph.portal import db as portal_db
from whygraph.portal import repos as portal_repos
from whygraph.portal import routes as routes_mod
from whygraph.portal import runner as runner_mod
from whygraph.portal.models import Project, ScanRun
from whygraph.portal.runner import ScanRunner, StaleCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _project_id(slug: str) -> int:
    with portal_db.get_session() as session:
        return session.exec(select(Project.id).where(Project.slug == slug)).one()


def _add_runs(slug: str, *summaries: str | None, status: str = "ok") -> None:
    """Insert finished runs of ``slug`` in order (the last is the newest)."""
    project_id = _project_id(slug)
    with portal_db.get_session() as session:
        for summary in summaries:
            session.add(
                ScanRun(
                    project_id=project_id,
                    trigger="manual",
                    status=status,
                    summary=summary,
                )
            )


def _coverage(commits: int, pct: float, cards: int, at: str) -> str:
    return json.dumps(
        {
            "coverage": {
                "commits": commits,
                "described": round(commits * pct / 100),
                "described_pct": pct,
                "pull_requests": 0,
                "issues": 0,
                "rationale_cards": cards,
                "at": at,
            }
        }
    )


# ---------------------------------------------------------------------------
# StaleCache (unit, section 6.1 #4)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Every ``git`` the runner module runs for ``stale``, recorded (and still run)."""
    calls: list[tuple[str, ...]] = []
    real = runner_mod._git_out

    def counting(root: Path, *args: str) -> str | None:
        calls.append(args)
        return real(root, *args)

    monkeypatch.setattr(runner_mod, "_git_out", counting)
    return calls


def test_stale_cache_memoises_head_for_ten_seconds(
    tmp_path: Path, git_calls: list[tuple[str, ...]]
) -> None:
    root = make_repo(tmp_path, "demo")
    first = _git(root, "rev-list", "--max-parents=0", "HEAD").strip()
    clock = FakeClock()
    cache = StaleCache(clock=clock)
    assert cache.info(1, root, None) is None and git_calls == []  # never scanned
    assert cache.info(1, root, first) == {"commits_behind": 1}
    assert [c[0] for c in git_calls] == ["rev-parse", "rev-list"]
    assert cache.info(1, root, first) == {"commits_behind": 1}
    assert len(git_calls) == 2  # HEAD memo and behind memo both hit

    (root / "sample.py").write_text("more\n")
    _git(root, "commit", "-qam", "third")
    clock.now += 9.9
    assert cache.info(1, root, first) == {"commits_behind": 1}  # HEAD still memoised
    assert len(git_calls) == 2
    clock.now += 0.2
    assert cache.info(1, root, first) == {"commits_behind": 2}
    assert [c[0] for c in git_calls[2:]] == ["rev-parse", "rev-list"]

    # Another project over the same checkout has its own memo.
    assert cache.info(2, root, first) == {"commits_behind": 2}
    assert len(git_calls) == 6
    head = _git(root, "rev-parse", "HEAD").strip()
    assert cache.info(2, root, head) is None  # up to date: no count
    assert len(git_calls) == 6


def test_stale_cache_keys_behind_by_head_and_last_scanned_and_evicts(
    tmp_path: Path, git_calls: list[tuple[str, ...]]
) -> None:
    root = make_repo(tmp_path, "demo")
    first = _git(root, "rev-list", "--max-parents=0", "HEAD").strip()
    cache = StaleCache(clock=FakeClock(), max_behind=2)
    cache.info(1, root, first)
    cache.info(1, root, "0" * 40)  # unknown commit: git cannot count
    assert cache.info(1, root, "0" * 40) == {"commits_behind": None}
    assert [c[0] for c in git_calls].count("rev-list") == 2
    cache.info(1, root, "1" * 40)  # a third key evicts the least recently used
    assert [c[0] for c in git_calls].count("rev-list") == 3
    cache.info(1, root, "0" * 40)  # still cached (used more recently than `first`)
    assert [c[0] for c in git_calls].count("rev-list") == 3
    cache.info(1, root, first)  # evicted: counted again
    assert [c[0] for c in git_calls].count("rev-list") == 4


def test_stale_cache_drop_forgets_one_project(
    tmp_path: Path, git_calls: list[tuple[str, ...]]
) -> None:
    root = make_repo(tmp_path, "demo")
    first = _git(root, "rev-list", "--max-parents=0", "HEAD").strip()
    cache = StaleCache(clock=FakeClock())
    cache.info(1, root, first)
    cache.info(2, root, first)
    assert len(git_calls) == 4
    cache.drop(1)
    cache.info(2, root, first)
    assert len(git_calls) == 4
    cache.info(1, root, first)
    assert [c[0] for c in git_calls[4:]] == ["rev-parse", "rev-list"]


def test_run_job_drops_the_projects_stale_entries(
    tmp_path: Path, git_calls: list[tuple[str, ...]]
) -> None:
    root = make_repo(tmp_path, "demo")
    first = _git(root, "rev-list", "--max-parents=0", "HEAD").strip()
    runner = ScanRunner()
    runner.stale = StaleCache(clock=FakeClock())
    runner.stale.info(7, root, first)
    runner.stale.info(8, root, first)
    assert len(git_calls) == 4
    ran: list[int] = []
    runner._execute = lambda job: ran.append(job.spec.run_id)  # type: ignore[method-assign]
    runner._dispatch = lambda: None  # type: ignore[method-assign]
    job = SimpleNamespace(spec=SimpleNamespace(project_id=7, run_id=42))

    async def run() -> None:
        runner._limiter = anyio.CapacityLimiter(1)
        await runner._run_job(job)  # type: ignore[arg-type]

    anyio.run(run)
    assert ran == [42]
    runner.stale.info(8, root, first)
    assert len(git_calls) == 4  # the other project kept its entries
    runner.stale.info(7, root, first)
    assert [c[0] for c in git_calls[4:]] == ["rev-parse", "rev-list"]


# ---------------------------------------------------------------------------
# GET /api/projects (section 6.2 #7)
# ---------------------------------------------------------------------------


def _statements(client: TestClient, path: str) -> int:
    """Statements the portal DB runs for one ``GET path`` (fewest of three tries).

    The lifespan's background tasks share the engine; the fewest of three
    requests leaves out a statement one of them happened to run meanwhile.
    """
    engine = portal_db.get_engine()
    counts = []
    for _ in range(3):
        seen: list[str] = []
        lock = threading.Lock()

        def count(conn, cursor, statement, *args) -> None:  # noqa: ANN001
            with lock:
                seen.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            assert client.get(path).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", count)
        counts.append(len(seen))
    return min(counts)


def test_list_runs_a_constant_number_of_statements(
    ready: TestClient, env: SimpleNamespace
) -> None:
    initialized_repo(ready, env, "p00")
    _add_runs("p00", _coverage(10, 50.0, 3, "2026-10-01T00:00:00+00:00"))
    one = _statements(ready, "/api/projects")
    for i in range(1, 10):
        name = f"p{i:02d}"
        initialized_repo(ready, env, name)
        _add_runs(name, _coverage(i, 10.0, i, "2026-10-01T00:00:00+00:00"), None)
        _add_runs(name, None, status="queued")
    listed = ready.get("/api/projects").json()["projects"]
    assert len(listed) == 10
    assert sum(p["running_scan"] is not None for p in listed) == 9
    assert sum(p["last_scan_stats"] is not None for p in listed) == 10
    assert _statements(ready, "/api/projects") == one


def test_last_scan_stats_from_the_newest_snapshot(
    ready: TestClient, env: SimpleNamespace
) -> None:
    initialized_repo(ready, env, "demo")
    initialized_repo(ready, env, "other")
    (listed,) = [
        p for p in ready.get("/api/projects").json()["projects"] if p["slug"] == "demo"
    ]
    # The first Initialize's scan left a (fake, empty) snapshot.
    assert listed["last_scan_stats"]["commits"] == 0 and listed["importing"] is False
    _add_runs(
        "demo",
        _coverage(10, 10.0, 1, "2026-10-01T00:00:00+00:00"),
        _coverage(20, 50.0, 7, "2026-10-02T00:00:00+00:00"),
        json.dumps({"error": 'boom "coverage" in the text'}),  # no coverage key
        "not json at all",  # never matches the pre-filter
    )
    # Only "other"'s newest snapshot-looking row is not JSON: skipped, never fatal.
    _add_runs(
        "other",
        _coverage(5, 100.0, 2, "2026-10-01T00:00:00+00:00"),
        '{"coverage": broken',
    )
    response = ready.get("/api/projects")
    assert response.status_code == 200
    by_slug = {p["slug"]: p for p in response.json()["projects"]}
    expected = {
        "commits": 20,
        "described_pct": 50.0,
        "rationale_cards": 7,
        "as_of": "2026-10-02T00:00:00+00:00",
    }
    assert by_slug["demo"]["last_scan_stats"] == expected
    assert by_slug["other"]["last_scan_stats"] is None
    details = ready.get("/api/projects/demo").json()
    assert details["last_scan_stats"] == expected
    assert details["importing"] is False
    # The details keep their live counts, in their own shape.
    assert set(details["stats"]) == {
        "commits",
        "described",
        "described_pct",
        "pull_requests",
        "issues",
        "rationale_cards",
    }
    assert details["root"] == str(env.shared / "demo")


def test_an_uninitialized_github_row_is_importing(
    ready: TestClient, env: SimpleNamespace
) -> None:
    with portal_db.get_session() as session:
        session.add(
            Project(
                org_id=builtin_org_id(session),
                slug="incoming",
                name="incoming",
                source="github",
                root="repos/incoming",
                remote_url="https://github.com/acme/incoming",
            )
        )
    root = make_repo(env.shared, "plain")
    add_local(ready, root)
    by_slug = {p["slug"]: p for p in ready.get("/api/projects").json()["projects"]}
    assert by_slug["incoming"]["importing"] is True
    assert by_slug["incoming"]["root_status"] == "missing"
    # A local project that is not set up yet is not importing.
    assert by_slug["plain"]["importing"] is False
    assert by_slug["plain"]["initialized"] is False
    # Local mode keeps every path (the user's own machine).
    assert by_slug["plain"]["root"] == str(root)


def test_stale_is_served_from_the_cache(
    ready: TestClient, env: SimpleNamespace, git_calls: list[tuple[str, ...]]
) -> None:
    root = initialized_repo(ready, env, "demo")
    first = _git(root, "rev-list", "--max-parents=0", "HEAD").strip()
    with portal_db.get_session() as session:
        row = session.exec(select(Project).where(Project.slug == "demo")).one()
        row.last_scanned_head = first
        session.add(row)
    clock = FakeClock()
    ready.app.state.portal.runner.stale = StaleCache(clock=clock)
    git_calls.clear()

    def stale() -> dict | None:
        (listed,) = ready.get("/api/projects").json()["projects"]
        return listed["stale"]

    assert stale() == {"commits_behind": 1}
    assert len(git_calls) == 2
    assert stale() == {"commits_behind": 1}
    assert ready.get("/api/projects/demo").json()["stale"] == {"commits_behind": 1}
    assert len(git_calls) == 2  # the 3 s poll runs no git within 10 s
    (root / "sample.py").write_text("more\n")
    _git(root, "commit", "-qam", "third")
    assert stale() == {"commits_behind": 1}  # HEAD memoised
    clock.now += 10.0
    assert stale() == {"commits_behind": 2}
    assert len(git_calls) == 4


# ---------------------------------------------------------------------------
# BUG-9: a missing path; BUG-20: discovery searches while walking
# ---------------------------------------------------------------------------


def test_a_missing_path_is_path_missing_not_not_git(
    ready: TestClient, env: SimpleNamespace
) -> None:
    missing = env.shared / "nope"
    check = ready.post("/api/portal/check-path", json={"path": str(missing)}).json()
    assert (check["exists"], check["shared"], check["is_git"]) == (False, True, False)
    response = ready.post(
        "/api/projects", json={"source": "local", "path": str(missing)}
    )
    assert (response.status_code, response.json()["code"]) == (400, "path_missing")
    # Outside a shared folder the portal cannot see the path: share it first.
    outside = env.tmp / "elsewhere" / "nope"
    response = ready.post(
        "/api/projects", json={"source": "local", "path": str(outside)}
    )
    assert response.json()["code"] == "not_shared"
    plain = env.shared / "plain"
    plain.mkdir()
    check = ready.post("/api/portal/check-path", json={"path": str(plain)}).json()
    assert (check["exists"], check["is_git"]) == (True, False)
    response = ready.post("/api/projects", json={"source": "local", "path": str(plain)})
    assert response.json()["code"] == "not_git"


def test_discovery_cap_counts_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    for name in ("alpha", "beta", "gamma"):
        make_repo(shared, name)
    make_repo(shared / "nested", "zeta")
    monkeypatch.setattr(portal_repos, "DISCOVERY_LIMIT", 2)
    assert len(portal_repos.discover_repos((shared,))) == 2
    # The walk order puts "nested/zeta" last: past the cap, still found.
    assert [p.name for p in portal_repos.discover_repos((shared,), "ZET")] == ["zeta"]
    # The needle matches the path too, not only the name.
    assert [p.name for p in portal_repos.discover_repos((shared,), "nested")] == [
        "zeta"
    ]


def test_discovery_cache_is_keyed_by_needle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = (tmp_path / "shared",)
    make_repo(shared[0], "alpha")
    walks: list[str] = []
    real = portal_repos.discover_repos

    def counting(folders: tuple[Path, ...], needle: str = "") -> list[Path]:
        walks.append(needle)
        return real(folders, needle)

    monkeypatch.setattr(portal_repos, "discover_repos", counting)
    cache = portal_repos.DiscoveryCache()
    assert [p.name for p in cache.get(shared, "alp")] == ["alpha"]
    assert [p.name for p in cache.get(shared, "ALP")] == ["alpha"]
    assert walks == ["alp"]
    assert cache.get(shared, "beta") == []
    assert walks == ["alp", "beta"]
    # A complete unfiltered listing answers later searches without a walk.
    assert [p.name for p in cache.get(shared)] == ["alpha"]
    assert cache.get(shared, "lph")[0].name == "alpha"
    assert walks == ["alp", "beta", ""]
    # ...but not once it was truncated at the cap.
    monkeypatch.setattr(portal_repos, "DISCOVERY_LIMIT", 1)
    cache.get(shared, "pha")
    assert walks == ["alp", "beta", "", "pha"]


def test_repos_route_searches_past_the_cap(
    ready: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("alpha", "beta", "gamma"):
        make_repo(env.shared, name)
    make_repo(env.shared / "nested", "zeta")
    monkeypatch.setattr(portal_repos, "DISCOVERY_LIMIT", 2)
    monkeypatch.setattr(routes_mod, "DISCOVERY_LIMIT", 2)
    body = ready.get("/api/portal/repos").json()
    assert len(body["repos"]) == 2 and body["truncated"] is True
    body = ready.get("/api/portal/repos?q=zeta").json()
    assert [r["name"] for r in body["repos"]] == ["zeta"]
    assert body["truncated"] is False
