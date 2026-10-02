"""The portal database: data directory, engine, instance lock and Alembic bootstrap.

The portal keeps its own tables (:mod:`whygraph.portal.models`) in
Postgres, reached through ``$WHYGRAPH_DATABASE_URL`` (plus an optional
``$WHYGRAPH_DATABASE_PASSWORD_FILE``), with its own Alembic chain
(``portal/migrations``). It is deliberately separate from every project's
``.whygraph/whygraph.db``, which stays SQLite in its repo: the two have
unrelated lifecycles, and a project database must stay usable by the
headless CLI without a portal.

The data directory is ``$WHYGRAPH_DATA`` when set, else
``~/.local/share/whygraph``. It holds ``secret.key``
(:mod:`whygraph.portal.secrets`), cloned repositories and scan run files,
which is why the paths stored in the database are relative to it, and -
until the one-time import has run - a 2.0 ``portal.db``
(:func:`legacy_db_path`).
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError
from sqlalchemy.pool import NullPool
from sqlmodel import Session

DATA_ENV_VAR = "WHYGRAPH_DATA"
"""Environment variable that overrides the data directory."""

DATABASE_URL_ENV = "WHYGRAPH_DATABASE_URL"
"""Environment variable holding the portal database URL (required)."""

DATABASE_PASSWORD_FILE_ENV = "WHYGRAPH_DATABASE_PASSWORD_FILE"
"""Optional file whose stripped contents become the URL's password."""

DATABASE_WAIT_ENV = "WHYGRAPH_DATABASE_WAIT_SEC"
"""Test / support knob: the default budget of :func:`wait_for_database`."""

LEGACY_DB_FILE_NAME = "portal.db"
"""The 2.0 SQLite portal file; only the one-time importer reads it."""

INSTANCE_LOCK_KEY = 0x7768796772617068
"""``b"whygraph"`` as a signed 64-bit int: the advisory lock of one portal."""

STATEMENT_TIMEOUT_MS = 30_000
IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000

_DEFAULT_WAIT_SEC = 60.0
_CONNECT_TIMEOUT_SEC = 5
_LOCK_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

_engines: dict[str, Engine] = {}
_engines_lock = threading.Lock()


class PortalDatabaseNotConfigured(RuntimeError):
    """``WHYGRAPH_DATABASE_URL`` is unset or not a Postgres URL.

    Also raised when ``WHYGRAPH_DATABASE_PASSWORD_FILE`` is set but cannot
    be read, so every configuration error maps to one CLI message.
    """


class PortalDatabaseUnreachable(RuntimeError):
    """The configured database did not answer within the wait budget.

    Attributes
    ----------
    target : str
        ``host:port/database`` of the URL - never its password.
    """

    def __init__(self, target: str, reason: str | None = None) -> None:
        self.target = target
        self.reason = reason
        message = f"portal database unreachable at {target}"
        super().__init__(f"{message}: {reason}" if reason else message)


def data_dir() -> Path:
    """Return the portal data directory, creating it (mode 700) if missing.

    Returns
    -------
    Path
        ``$WHYGRAPH_DATA`` when set and non-empty, else
        ``~/.local/share/whygraph``. An existing directory keeps its mode.
    """
    override = os.environ.get(DATA_ENV_VAR)
    path = Path(override).expanduser() if override else _default_data_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _default_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "whygraph"


def legacy_db_path() -> Path:
    """Return the path of a 2.0 SQLite ``portal.db`` inside :func:`data_dir`."""
    return data_dir() / LEGACY_DB_FILE_NAME


def database_url() -> URL:
    """Resolve the portal database URL from the environment.

    ``postgres://`` and ``postgresql://`` (any driver) are normalised to
    ``postgresql+psycopg://``. When ``$WHYGRAPH_DATABASE_PASSWORD_FILE`` is
    set, its contents with surrounding whitespace stripped replace any
    password in the URL.

    Returns
    -------
    URL
        The URL to connect with. Never pass ``str()`` of it as a DSN
        (SQLAlchemy masks the password there); pass the object itself.

    Raises
    ------
    PortalDatabaseNotConfigured
        When the variable is unset or empty, is not a Postgres URL, or the
        password file is set but missing or unreadable.
    """
    raw = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not raw:
        raise PortalDatabaseNotConfigured(f"{DATABASE_URL_ENV} is not set")
    # SQLAlchemy has no "postgres" dialect: make_url parses it, create_engine fails.
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    try:
        url = make_url(raw)
    except ArgumentError:
        raise PortalDatabaseNotConfigured(
            f"{DATABASE_URL_ENV} is not a valid database URL"
        ) from None
    if url.get_backend_name() != "postgresql":
        raise PortalDatabaseNotConfigured(
            f"{DATABASE_URL_ENV} must be a Postgres URL, not {url.get_backend_name()!r}"
        )
    url = url.set(drivername="postgresql+psycopg")
    password_file = os.environ.get(DATABASE_PASSWORD_FILE_ENV, "").strip()
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            raise PortalDatabaseNotConfigured(
                f"cannot read {DATABASE_PASSWORD_FILE_ENV} ({password_file}): {reason}"
            ) from None
        url = url.set(password=password)
    return url


def database_target(url: URL | None = None) -> str:
    """Return ``host:port/database`` of the portal URL, for messages.

    Parameters
    ----------
    url : URL, optional
        Defaults to :func:`database_url`.

    Returns
    -------
    str
        Never includes the user or the password.
    """
    url = url if url is not None else database_url()
    return f"{url.host or 'localhost'}:{url.port or 5432}/{url.database or ''}"


def _build_engine(url: URL, *, timeouts: bool = True) -> Engine:
    """The portal's pooled engine; *timeouts* adds the server-side limits."""
    options = (
        f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
        f"-c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT_MS}"
    )
    connect_args: dict[str, object] = {
        "connect_timeout": _CONNECT_TIMEOUT_SEC,
        "application_name": "whygraph-portal",
    }
    if timeouts:
        connect_args["options"] = options
    return create_engine(
        url,
        pool_pre_ping=True,  # a Postgres restart must not leave dead connections
        pool_recycle=1800,  # and neither may a NAT / firewall idle drop
        pool_size=10,  # ~40 threadpool workers; Postgres default max_connections=100
        max_overflow=20,
        connect_args=connect_args,
    )


def get_engine() -> Engine:
    """Return the cached pooled engine for the portal database.

    Its connections carry ``statement_timeout`` (30 s) and
    ``idle_in_transaction_session_timeout`` (60 s), so a stuck request
    cannot hold a connection or a lock forever.

    Returns
    -------
    Engine
        One engine per resolved URL; repeated calls return it.

    Raises
    ------
    PortalDatabaseNotConfigured
        See :func:`database_url`.
    """
    url = database_url()
    # The key never leaves the process; it only separates engines per URL.
    key = url.render_as_string(hide_password=False)
    engine = _engines.get(key)
    if engine is None:
        with _engines_lock:
            engine = _engines.get(key)
            if engine is None:
                engine = _engines[key] = _build_engine(url)
    return engine


def migration_engine() -> Engine:
    """Return a new ``NullPool`` engine without the server timeouts.

    For Alembic and the one-time import: a baseline on a slow disk, or a
    large import transaction, must not hit ``statement_timeout`` /
    ``idle_in_transaction_session_timeout``. Built per call; the caller
    disposes it.

    Returns
    -------
    Engine
        An uncached engine on :func:`database_url`.
    """
    return create_engine(
        database_url(),
        poolclass=NullPool,
        connect_args={
            "connect_timeout": _CONNECT_TIMEOUT_SEC,
            "application_name": "whygraph-portal-migrate",
        },
    )


def _reset_engine() -> None:
    """Dispose and drop every cached engine. Test-only - not public API."""
    with _engines_lock:
        engines = list(_engines.values())
        _engines.clear()
    for engine in engines:
        engine.dispose()


def wait_for_database(timeout: float | None = None) -> None:
    """Block until the portal database answers ``SELECT 1``.

    Retries with backoff (0.5 s doubling to 2 s), for the unordered
    restarts after a reboot, when the portal can start before Postgres.

    Parameters
    ----------
    timeout : float, optional
        Seconds to keep trying. Defaults to ``$WHYGRAPH_DATABASE_WAIT_SEC``,
        else 60.

    Raises
    ------
    PortalDatabaseUnreachable
        When no attempt succeeded within *timeout*.
    PortalDatabaseNotConfigured
        See :func:`database_url`.
    """
    if timeout is None:
        timeout = _wait_budget()
    url = database_url()
    deadline = time.monotonic() + timeout
    delay = 0.5
    while True:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except DBAPIError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = str(exc.orig).strip().splitlines()[0] if exc.orig else None
                raise PortalDatabaseUnreachable(database_target(url), reason) from None
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 2.0)


def _wait_budget() -> float:
    raw = os.environ.get(DATABASE_WAIT_ENV, "").strip()
    try:
        return float(raw) if raw else _DEFAULT_WAIT_SEC
    except ValueError:
        return _DEFAULT_WAIT_SEC


class InstanceLock:
    """``pg_try_advisory_lock(INSTANCE_LOCK_KEY)`` on a dedicated connection.

    Guards "one portal per database" the way ``portal.lock`` guards one per
    data dir. The lock lives as long as its session, so it is held on a
    ``NullPool`` connection of its own (a pooled one can be recycled or
    reset under it), in autocommit (never idle in a transaction), without
    the server timeouts, and with TCP keepalives so a half-dead peer is
    noticed.

    Notes
    -----
    Call :meth:`acquire` once per process: advisory locks are re-entrant,
    so N acquires would need N unlocks. If the session dies (a Postgres
    restart, a dropped connection), the lock is gone; :meth:`is_held`
    reports that.
    """

    def __init__(self, key: int = INSTANCE_LOCK_KEY) -> None:
        self._key = key
        self._engine: Engine | None = None
        self._conn: Connection | None = None
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """Try to take the lock without waiting.

        Returns
        -------
        bool
            ``True`` when this process now holds it, ``False`` when another
            session does.

        Raises
        ------
        RuntimeError
            When this instance already holds the lock.
        sqlalchemy.exc.DBAPIError
            When the database cannot be reached.
        """
        with self._lock:
            if self._conn is not None:
                raise RuntimeError("the instance lock is already held")
            engine = create_engine(
                database_url(),
                poolclass=NullPool,
                isolation_level="AUTOCOMMIT",
                connect_args={
                    "connect_timeout": _CONNECT_TIMEOUT_SEC,
                    "application_name": "whygraph-portal-lock",
                    **_LOCK_KEEPALIVES,
                },
            )
            conn: Connection | None = None
            try:
                conn = engine.connect()
                got = conn.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
                    {"key": self._key},
                ).scalar()
            except BaseException:
                if conn is not None:
                    conn.close()
                engine.dispose()
                raise
            if not got:
                conn.close()
                engine.dispose()
                return False
            self._engine, self._conn = engine, conn
            return True

    def is_held(self) -> bool:
        """Return whether the lock's session is still alive.

        Returns
        -------
        bool
            ``False`` when the lock was never taken, was released, or its
            connection fails a ``SELECT 1`` (the session, and with it the
            lock, is gone).
        """
        with self._lock:
            if self._conn is None:
                return False
            try:
                self._conn.execute(text("SELECT 1"))
            except Exception:
                return False
            return True

    def release(self) -> None:
        """Unlock and close the lock connection; a no-op when not held."""
        with self._lock:
            conn, engine = self._conn, self._engine
            self._conn = self._engine = None
            if conn is None:
                return
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"),
                    {"key": self._key},
                )
            except Exception:
                pass  # a dead session already dropped the lock
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
                if engine is not None:
                    engine.dispose()


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a :class:`sqlmodel.Session` on the portal DB.

    Commits on normal exit, rolls back on an exception, always closes.

    Yields
    ------
    Session
        A fresh session - never share one across threads.
    """
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def alembic_config() -> AlembicConfig:
    """Build an :class:`AlembicConfig` for the packaged portal migrations.

    The script location is resolved from the installed package; ``env.py``
    connects through :func:`migration_engine`, so no URL is set here.

    Returns
    -------
    AlembicConfig
        A config for the portal's own migration chain.
    """
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return cfg


def ensure_initialized() -> None:
    """Idempotently bring the portal DB schema up to head.

    Raises
    ------
    PortalDatabaseNotConfigured
        See :func:`database_url`.
    """
    command.upgrade(alembic_config(), "head")


__all__ = [
    "DATABASE_PASSWORD_FILE_ENV",
    "DATABASE_URL_ENV",
    "InstanceLock",
    "PortalDatabaseNotConfigured",
    "PortalDatabaseUnreachable",
    "alembic_config",
    "data_dir",
    "database_target",
    "database_url",
    "ensure_initialized",
    "get_engine",
    "get_session",
    "legacy_db_path",
    "migration_engine",
    "wait_for_database",
]
