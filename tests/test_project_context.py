"""Tests for the project-context seam (:mod:`whygraph.core.context`).

Covers the M1 step 1 contract: interleaved :func:`use_project` blocks
over two repos each read their own config, root and DB; strict mode
fails closed with no context bound; a :class:`Crawler` thread and the
:class:`AnalyzeCrawler` worker pool both see the creating context (the
pool with a fresh context copy per submit, so two workers never enter
one shared :class:`contextvars.Context`).
"""

from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from rich.progress import Progress
from sqlalchemy import text
from sqlmodel import select

from whygraph import core
from whygraph.analyze import Description
from whygraph.core import _resolve_root, get_config
from whygraph.core.config import Config
from whygraph.core.context import (
    ProjectContext,
    ProjectContextError,
    current_project,
    is_strict,
    set_strict,
    use_project,
)
from whygraph.db import engine as db_engine
from whygraph.db import ensure_initialized, get_engine, get_session
from whygraph.db.models.commit import Commit as CommitRow
from whygraph.mcp.targets import repo_root
from whygraph.scan import AnalyzeCrawler
from whygraph.scan.crawler import Crawler
from whygraph.services.git import Repository
from whygraph.services.git.commits import Commits


@pytest.fixture(autouse=True)
def _reset_seam() -> Iterator[None]:
    """Reset strict mode and the config / engine caches around each test."""
    set_strict(False)
    db_engine._reset_engine()
    core._reset_config()
    try:
        yield
    finally:
        set_strict(False)
        db_engine._reset_engine()
        core._reset_config()


@dataclass
class _Projects:
    a: ProjectContext
    b: ProjectContext


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_git_repo(root: Path) -> None:
    """Initialize ``root`` as a git repo with three commits on ``main``."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    for name, body in (("a.txt", "one\n"), ("b.txt", "two\n"), ("a.txt", "three\n")):
        (root / name).write_text(body)
        _git(root, "add", name)
        _git(root, "commit", "-q", "-m", f"edit {name}")


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Projects:
    """Two initialized repos: ``a`` on the default DB path, ``b`` on an override.

    ``cwd`` is a third directory with no ``.git``, so any lookup that
    ignored the context would land somewhere neither project owns.
    """
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    _make_git_repo(root_a)
    _make_git_repo(root_b)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    a = ProjectContext(slug="a", root=root_a, config=Config())
    b = ProjectContext(
        slug="b",
        root=root_b,
        config=Config(whygraph_db=root_b / "custom" / "b.db"),
    )
    for ctx in (a, b):
        with use_project(ctx):
            ensure_initialized()
            with get_session() as session:
                session.exec(text("CREATE TABLE marker (v TEXT)"))
                session.exec(
                    text("INSERT INTO marker VALUES (:v)"), params={"v": ctx.slug}
                )
    return _Projects(a=a, b=b)


def _marker() -> str:
    with get_session() as session:
        return session.exec(text("SELECT v FROM marker")).one()[0]


# --- interleaved projects ---------------------------------------------------


def test_interleaved_projects_read_their_own_config_root_and_db(
    projects: _Projects, tmp_path: Path
) -> None:
    a, b = projects.a, projects.b

    with use_project(a):
        assert current_project() is a
        assert get_config() is a.config
        assert _resolve_root() == a.root
        assert repo_root() == a.root
        assert get_engine().url.database == str(a.root / ".whygraph" / "whygraph.db")
        assert _marker() == "a"

        with use_project(b):
            assert current_project() is b
            assert get_config() is b.config
            assert _resolve_root() == b.root
            assert get_engine().url.database == str(b.root / "custom" / "b.db")
            assert _marker() == "b"

        # Leaving the inner block restores the outer project.
        assert current_project() is a
        assert get_config() is a.config
        assert _marker() == "a"

    with use_project(b):
        assert _marker() == "b"
    with use_project(a):
        assert _marker() == "a"

    assert current_project() is None
    # Both DBs were migrated in place; nothing was created around cwd.
    assert (a.root / ".whygraph" / "whygraph.db").exists()
    assert (b.root / "custom" / "b.db").exists()
    assert not (b.root / ".whygraph").exists()
    assert not (tmp_path / "elsewhere" / ".whygraph").exists()


def test_engine_cached_per_project(projects: _Projects) -> None:
    with use_project(projects.a):
        engine_a = get_engine()
        assert get_engine() is engine_a
    with use_project(projects.b):
        engine_b = get_engine()
    assert engine_b is not engine_a
    with use_project(projects.a):
        assert get_engine() is engine_a

    db_engine._reset_engine()
    with use_project(projects.a):
        assert get_engine() is not engine_a


def test_context_config_bypasses_global_cache(
    projects: _Projects, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A bound context neither reads nor fills the process-wide cache.
    with use_project(projects.a):
        assert get_config() is projects.a.config
    assert core._config is None

    cli_config = Config(whygraph_db=tmp_path / "cli.db")
    monkeypatch.setattr(core, "_config", cli_config)
    with use_project(projects.b):
        assert get_config() is projects.b.config
    assert get_config() is cli_config


# --- strict mode ---------------------------------------------------------------


def test_strict_mode_is_off_by_default() -> None:
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "from whygraph.core import context; print(context.is_strict())",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "False"


def test_no_context_without_strict_falls_back_to_cwd(
    projects: _Projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = projects.a.root / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    assert not is_strict()
    assert current_project() is None
    assert _resolve_root() == projects.a.root
    assert get_engine().url.database == str(
        projects.a.root / ".whygraph" / "whygraph.db"
    )
    assert _marker() == "a"


def test_strict_mode_raises_without_context(projects: _Projects) -> None:
    set_strict(True)
    assert is_strict()

    # Inspecting the binding never raises.
    assert current_project() is None
    with pytest.raises(ProjectContextError, match="get_config"):
        get_config()
    with pytest.raises(ProjectContextError, match="_resolve_root"):
        _resolve_root()
    with pytest.raises(ProjectContextError):
        repo_root()
    with pytest.raises(ProjectContextError):
        get_engine()
    with pytest.raises(ProjectContextError):
        with get_session():
            pass

    # With a context bound, strict mode is invisible.
    with use_project(projects.b):
        assert get_config() is projects.b.config
        assert _marker() == "b"

    # And it fails closed again once the block exits.
    with pytest.raises(ProjectContextError):
        get_config()


def test_strict_mode_plain_thread_does_not_inherit_context(
    projects: _Projects,
) -> None:
    """Control for §2.1: a bare thread falls out of the context and fails closed."""
    set_strict(True)
    caught: list[BaseException] = []

    def probe() -> None:
        try:
            get_config()
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            caught.append(exc)

    with use_project(projects.a):
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()

    assert len(caught) == 1
    assert isinstance(caught[0], ProjectContextError)


# --- crawler propagation ---------------------------------------------------


class _ProbeCrawler(Crawler):
    """Records what the project-scoped lookups return inside the thread."""

    def work(self) -> None:
        self.seen = (current_project(), get_config(), _resolve_root(), _marker())


def test_crawler_thread_sees_creators_context(projects: _Projects) -> None:
    set_strict(True)
    with Progress(disable=True) as progress:
        with use_project(projects.a):
            crawler = _ProbeCrawler("probe", progress)
        # Started under a *different* project: the snapshot taken at
        # construction wins, not whatever is bound at start time.
        with use_project(projects.b):
            crawler.start()
            crawler.join()

    assert crawler.error is None
    ctx, config, root, marker = crawler.seen
    assert ctx is projects.a
    assert config is projects.a.config
    assert root == projects.a.root
    assert marker == "a"


class _ContextProbeDescriptor:
    """Stub LLM descriptor that does a strict-mode config read per call.

    The first two calls rendezvous on a barrier, so two pool workers are
    provably inside the per-submit context copies at the same time - the
    case where reusing one ``Context`` would raise ``RuntimeError``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(2, timeout=10)
        self._calls = 0
        self.seen: list[tuple[str | None, Config, str]] = []

    def describe(self, diff: str) -> Description:
        with self._lock:
            call = self._calls
            self._calls += 1
        if call < 2:
            self._barrier.wait()
        ctx = current_project()
        config = get_config()
        with self._lock:
            self.seen.append(
                (ctx.slug if ctx else None, config, threading.current_thread().name)
            )
        return Description(
            text="DESCRIPTION",
            model="stub-model",
            provider="stub-provider",
            input_tokens=1,
            output_tokens=2,
        )


def _insert_pending_commits(root: Path) -> list:
    commits = list(Commits(root, "main"))
    with get_session() as session:
        for c in commits:
            session.add(
                CommitRow(
                    sha=c.sha,
                    parent_shas=" ".join(c.parent_shas),
                    author_name="Test User",
                    author_email="test@example.com",
                    authored_at="2026-01-01T00:00:00+00:00",
                    committed_at="2026-01-01T00:00:00+00:00",
                    subject=c.subject,
                    body="",
                    files_changed=0,
                    insertions=0,
                    deletions=0,
                    scanned_at="2026-01-01T00:00:00+00:00",
                    llm_description=None,
                )
            )
    return commits


def test_analyze_pool_workers_see_context_in_strict_mode(
    projects: _Projects,
) -> None:
    a = projects.a
    with use_project(a):
        commits = _insert_pending_commits(a.root)

    set_strict(True)
    descriptor = _ContextProbeDescriptor()
    with Progress(disable=True) as progress:
        with use_project(a):
            crawler = AnalyzeCrawler(
                progress,
                repository=Repository(a.root),
                descriptor=descriptor,
                max_workers=2,
                large_commit_file_count=10_000,
            )
        crawler.start()
        crawler.join()

    assert crawler.error is None, crawler.error
    assert len(descriptor.seen) == len(commits)
    assert {slug for slug, _, _ in descriptor.seen} == {"a"}
    assert all(config is a.config for _, config, _ in descriptor.seen)
    # The work really fanned out across more than one pool worker.
    assert len({thread for _, _, thread in descriptor.seen}) >= 2

    # The workers wrote to project a's DB, not project b's.
    with use_project(a):
        with get_session() as session:
            descriptions = {r.llm_description for r in session.exec(select(CommitRow))}
    assert descriptions == {"DESCRIPTION"}
    with use_project(projects.b):
        with get_session() as session:
            assert session.exec(select(CommitRow)).all() == []
