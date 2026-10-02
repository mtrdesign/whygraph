from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterable, Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import NullPool

from whygraph import core
from whygraph.cli.commands.install import POSTGRES_IMAGE
from whygraph.core.config import Config
from whygraph.core.context import set_strict
from whygraph.db import ensure_initialized
from whygraph.db import engine as db_engine
from whygraph.portal import db as portal_db

_CODEGRAPH_SCHEMA = """\
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    docstring TEXT,
    signature TEXT
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER
);
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    language TEXT
);
"""


@pytest.fixture(autouse=True)
def _reset_process_globals() -> Iterator[None]:
    """Reset the process-wide state a portal app (or a test) may leave behind.

    Strict project-context mode is a process-global flag the portal lifespan
    turns on; the engine dicts and their pooled connections are per process.
    Resetting them after every test keeps a portal test from leaking into a
    later CLI test.
    """
    yield
    set_strict(False)
    db_engine._reset_engine()
    portal_db._reset_engine()


# An edge fixture row: (source, target, kind), or (source, target, kind, line)
# when a test needs to exercise the edge's recorded line.
EdgeRow = tuple[str, str, str] | tuple[str, str, str, int | None]


_NODE_FIELDS = (
    "id",
    "kind",
    "name",
    "qualified_name",
    "file_path",
    "language",
    "start_line",
    "end_line",
    "docstring",
    "signature",
)


def _insert_nodes(conn: sqlite3.Connection, nodes: Iterable[dict]) -> None:
    placeholders = ",".join("?" * len(_NODE_FIELDS))
    conn.executemany(
        f"INSERT INTO nodes({', '.join(_NODE_FIELDS)}) VALUES ({placeholders})",
        [tuple(n.get(f) for f in _NODE_FIELDS) for n in nodes],
    )


def _insert_edges(conn: sqlite3.Connection, edges: Iterable[EdgeRow]) -> None:
    rows = [(e[0], e[1], e[2], e[3] if len(e) > 3 else None) for e in edges]
    conn.executemany(
        "INSERT INTO edges(source, target, kind, line) VALUES (?, ?, ?, ?)",
        rows,
    )


def build_fake_codegraph_db(
    path: Path,
    *,
    nodes: list[dict] | None = None,
    edges: list[EdgeRow] | None = None,
) -> Path:
    """Create a minimal CodeGraph-shaped SQLite DB for tests.

    Default fixture: three nodes (a, b, c) where a calls b and b calls c.
    """
    if nodes is None:
        nodes = [
            {
                "id": "n_a",
                "kind": "function",
                "name": "a",
                "qualified_name": "pkg.a",
                "file_path": "src/pkg/a.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": "doc-a",
                "signature": "def a()",
            },
            {
                "id": "n_b",
                "kind": "function",
                "name": "b",
                "qualified_name": "pkg.b",
                "file_path": "src/pkg/b.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": None,
                "signature": "def b()",
            },
            {
                "id": "n_c",
                "kind": "function",
                "name": "c",
                "qualified_name": "pkg.c",
                "file_path": "src/pkg/c.py",
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": None,
                "signature": "def c()",
            },
        ]
    if edges is None:
        edges = [("n_a", "n_b", "calls"), ("n_b", "n_c", "calls")]

    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_CODEGRAPH_SCHEMA)
        _insert_nodes(conn, nodes)
        _insert_edges(conn, edges)
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def fake_codegraph_db(tmp_path: Path) -> Path:
    return build_fake_codegraph_db(tmp_path / "codegraph.db")


@pytest.fixture
def codegraph_db_factory(tmp_path: Path):
    counter = {"n": 0}

    def _factory(
        *,
        nodes: list[dict] | None = None,
        edges: list[EdgeRow] | None = None,
    ) -> Path:
        counter["n"] += 1
        path = tmp_path / f"codegraph_{counter['n']}.db"
        return build_fake_codegraph_db(path, nodes=nodes, edges=edges)

    return _factory


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one file committed across two commits.

    ``sample.py`` ends with three lines: the first two land in the initial
    commit, the third in the second commit — so a blame of lines 1-3
    yields two distinct commits.
    """
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        )

    _git("init")
    _git("config", "user.email", "tester@example.com")
    _git("config", "user.name", "Test User")
    _git("config", "commit.gpgsign", "false")
    sample = root / "sample.py"
    sample.write_text("line one\nline two\n")
    _git("add", "sample.py")
    _git("commit", "-m", "first commit")
    sample.write_text("line one\nline two\nline three\n")
    _git("add", "sample.py")
    _git("commit", "-m", "second commit\n\nAdds the third line for context.")
    return root


@pytest.fixture
def whygraph_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point WhyGraph's DB layer at an isolated, empty per-test SQLite file.

    Yields the path. The schema is *not* created — use
    :func:`whygraph_db_initialized` for a migrated database.
    """
    db_path = tmp_path / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


@pytest.fixture
def whygraph_db_initialized(whygraph_db: Path) -> Path:
    """An isolated WhyGraph DB with the schema migrated to head."""
    ensure_initialized()
    return whygraph_db


# ---------------------------------------------------------------------------
# The portal database: a Postgres server per session, a database per test
# ---------------------------------------------------------------------------

TEST_DATABASE_URL_ENV = "WHYGRAPH_TEST_DATABASE_URL"
TEST_POSTGRES_IMAGE_ENV = "WHYGRAPH_TEST_POSTGRES_IMAGE"
_PG_LABEL = "whygraph.test-pg"
_PG_PID_LABEL = "whygraph.test-pg.pid"
_PG_READY_SEC = 30.0
_NO_POSTGRES = (
    "the portal tests need a Postgres: start Docker (pytest then runs a throwaway "
    f"{POSTGRES_IMAGE} container), or set {TEST_DATABASE_URL_ENV} to an admin URL "
    "(CREATEDB), e.g. postgresql+psycopg://postgres:test@127.0.0.1:5432/postgres"
)


def _pg_url(raw: str) -> URL:
    """Parse a Postgres URL the way the portal does (``postgres://`` and psycopg)."""
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    return make_url(raw).set(drivername="postgresql+psycopg")


def _url_string(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep_stale_pg_containers() -> None:
    """Remove labelled test servers whose pytest process is gone (a Ctrl+C'd run)."""
    listing = _docker(
        "ps",
        "-a",
        "--filter",
        f"label={_PG_LABEL}=1",
        "--format",
        f'{{{{.ID}}}} {{{{.Label "{_PG_PID_LABEL}"}}}}',
    )
    for line in listing.stdout.splitlines():
        cid, _, pid = line.partition(" ")
        if not pid.strip().isdigit() or not _pid_alive(int(pid)):
            _docker("rm", "-f", cid)


def _wait_until_ready(url: URL, container: str) -> None:
    """Ready = a real TCP connect plus ``SELECT 1`` (not the init-time server)."""
    import psycopg

    deadline = time.monotonic() + _PG_READY_SEC
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                host=url.host,
                port=url.port,
                user=url.username,
                password=url.password,
                dbname=url.database,
                connect_timeout=2,
            ) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            last = exc
            time.sleep(0.2)
    logs = _docker("logs", "--tail", "20", container)
    pytest.fail(
        f"the throwaway Postgres ({container}) was not ready within "
        f"{_PG_READY_SEC:.0f}s: {last}\n{logs.stdout}{logs.stderr}",
        pytrace=False,
    )


@pytest.fixture(scope="session")
def postgres_admin_url() -> Iterator[str]:
    """An admin URL: ``$WHYGRAPH_TEST_DATABASE_URL``, else a throwaway container.

    The container (``$WHYGRAPH_TEST_POSTGRES_IMAGE`` or the pinned
    ``POSTGRES_IMAGE``) is labelled with this process's pid, so a later
    session sweeps it if this one is killed; it is removed at teardown and,
    as a second line, at interpreter exit. Without Docker or a URL the
    requesting tests **fail** - a portal test is never skipped.
    """
    configured = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if configured:
        yield _url_string(_pg_url(configured))
        return
    if shutil.which("docker") is None:
        pytest.fail(_NO_POSTGRES, pytrace=False)
    _sweep_stale_pg_containers()
    image = os.environ.get(TEST_POSTGRES_IMAGE_ENV, "").strip() or POSTGRES_IMAGE
    name = f"whygraph-test-pg-{os.getpid()}"
    started = _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--label",
        f"{_PG_LABEL}=1",
        "--label",
        f"{_PG_PID_LABEL}={os.getpid()}",
        "-p",
        "127.0.0.1::5432",
        "-e",
        "POSTGRES_PASSWORD=test",
        "--tmpfs",
        "/var/lib/postgresql",
        image,
        "-c",
        "fsync=off",
        "-c",
        "synchronous_commit=off",
        "-c",
        "full_page_writes=off",
    )
    if started.returncode != 0:
        pytest.fail(
            f"{_NO_POSTGRES}\n(docker run {image} failed: {started.stderr.strip()})",
            pytrace=False,
        )
    atexit.register(_docker, "rm", "-f", name)
    try:
        mapped = _docker("port", name, "5432/tcp").stdout.split()
        if not mapped:
            pytest.fail(f"docker port {name} reported no mapping", pytrace=False)
        port = int(mapped[0].rsplit(":", 1)[1])
        url = make_url(f"postgresql+psycopg://postgres:test@127.0.0.1:{port}/postgres")
        _wait_until_ready(url, name)
        yield _url_string(url)
    finally:
        _docker("rm", "-f", name)


@pytest.fixture(scope="session")
def _postgres_admin_engine(postgres_admin_url: str) -> Iterator[Engine]:
    """An AUTOCOMMIT engine: ``CREATE`` / ``DROP DATABASE`` refuse a transaction."""
    engine = create_engine(
        postgres_admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        yield engine
    finally:
        engine.dispose()


def _database_url(admin_url: str, name: str) -> str:
    return _url_string(make_url(admin_url).set(database=name))


def _drop_database(admin: Engine, name: str) -> None:
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def portal_template_db(
    postgres_admin_url: str, _postgres_admin_engine: Engine
) -> Iterator[str]:
    """A database migrated to head once per session, cloned by every test.

    Named per process, so two sessions sharing one server never collide.
    Every connection to it is closed before it is yielded.
    """
    name = f"whygraph_tpl_{os.getpid()}"
    _drop_database(_postgres_admin_engine, name)
    with _postgres_admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(portal_db.DATABASE_URL_ENV, _database_url(postgres_admin_url, name))
        mp.delenv(portal_db.DATABASE_PASSWORD_FILE_ENV, raising=False)
        portal_db.ensure_initialized()
        portal_db._reset_engine()
    try:
        yield name
    finally:
        _drop_database(_postgres_admin_engine, name)


def _clone_database(admin: Engine, template: str) -> str:
    name = f"t_{uuid.uuid4().hex}"
    with admin.connect() as conn:
        # A stray connection to the template makes CREATE DATABASE fail or hang.
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": template},
        )
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
    return name


def _use_database(
    admin_url: str,
    admin: Engine,
    template: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    name = _clone_database(admin, template)
    url = _database_url(admin_url, name)
    monkeypatch.setenv(portal_db.DATABASE_URL_ENV, url)
    monkeypatch.delenv(portal_db.DATABASE_PASSWORD_FILE_ENV, raising=False)
    portal_db._reset_engine()
    try:
        yield url
    finally:
        portal_db._reset_engine()
        _drop_database(admin, name)


@pytest.fixture
def portal_database(
    postgres_admin_url: str,
    _postgres_admin_engine: Engine,
    portal_template_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """A fresh, migrated portal database for this test; yields its URL.

    Cloned from :func:`portal_template_db` (milliseconds, not a migration)
    and bound through ``$WHYGRAPH_DATABASE_URL``; dropped at teardown.
    """
    yield from _use_database(
        postgres_admin_url, _postgres_admin_engine, portal_template_db, monkeypatch
    )


@pytest.fixture
def empty_portal_database(
    postgres_admin_url: str,
    _postgres_admin_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Like :func:`portal_database`, but unmigrated (cloned from ``template0``)."""
    yield from _use_database(
        postgres_admin_url, _postgres_admin_engine, "template0", monkeypatch
    )


def builtin_org_id(session) -> int:  # noqa: ANN001
    """The built-in org's id, created (with a local ``settings`` row) if missing.

    Portal DB tests run on a migrated database with no lifespan, so nothing
    has seeded the org yet; this does what the portal's start would.
    """
    from whygraph.portal.models import Setting
    from whygraph.portal.orgs import ensure_builtin_org

    setting = session.get(Setting, 1)
    if setting is None:
        setting = Setting(id=1, mode="local")
        session.add(setting)
    org = ensure_builtin_org(session, setting)
    assert org.id is not None
    return org.id
