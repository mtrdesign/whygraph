"""The ``whygraph portal`` subcommand - run the multi-project portal server.

This is the command the Docker runtime starts inside the image
(``whygraph up`` runs it with ``--host 0.0.0.0`` behind a loopback-only
publish). Run natively it is **dev-only** in this release: there is no
``--folder`` flag (shared folders come from ``WHYGRAPH_SHARED_FOLDERS``),
and a non-loopback ``--host`` outside the image is refused unless
``--dev-expose`` is given (the CLI cannot know the stored mode, and local
mode has no login).

``WHYGRAPH_TRUSTED_PROXIES`` (comma-separated IPs / CIDRs) becomes uvicorn's
``forwarded_allow_ips``; unset trusts no proxy, and a malformed value exits
2 before the app starts, as does a :class:`~whygraph.portal.app.PortalStartupError`
(a refused production environment or a mode mismatch).

The portal's own data lives in Postgres since 2.1: ``WHYGRAPH_DATABASE_URL``
(plus an optional ``WHYGRAPH_DATABASE_PASSWORD_FILE``) must name it - the
shim sets both - or the command exits 2. A database that stays unreachable
through the start-up wait exits 3, so ``--restart unless-stopped`` retries.

It runs exactly one uvicorn process - the scan runner, migration lock and
caches are in-process - with ``timeout_graceful_shutdown`` bounded below
the ``whygraph down`` grace period, and through
:class:`~whygraph.portal.app.PortalServer`, which ends open event streams
as soon as shutdown begins, so they cannot hold up a stop.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

IN_IMAGE_ENV = "WHYGRAPH_IN_IMAGE"
"""Baked ``=1`` into the Docker image (``docker/whygraph/Dockerfile``)."""

GRACEFUL_SHUTDOWN_SEC = 10
"""uvicorn's graceful-shutdown bound; below ``whygraph down``'s 30 s grace."""

EXIT_DATABASE_UNREACHABLE = 3
"""Exit code when the portal database stays unreachable (the restart policy retries)."""

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _default_port() -> int:
    raw = os.environ.get("WHYGRAPH_PORT", "").strip()
    return int(raw) if raw.isdigit() else 8765


@click.command(name="portal")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address (the Docker runtime passes 0.0.0.0).",
)
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    default=_default_port,
    show_default="$WHYGRAPH_PORT or 8765",
    help="Port to bind; also the port agents and the browser use.",
)
@click.option(
    "--data",
    "data_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Portal data directory (default: $WHYGRAPH_DATA or ~/.local/share/whygraph).",
)
@click.option(
    "--dev-expose",
    is_flag=True,
    help="Allow a non-loopback --host outside the Docker image (development only).",
)
def portal_cmd(host: str, port: int, data_dir: Path | None, dev_expose: bool) -> None:
    """Run the WhyGraph portal (use `whygraph up` to start it in Docker)."""
    in_image = os.environ.get(IN_IMAGE_ENV) == "1"
    if host not in _LOOPBACK and not in_image and not dev_expose:
        click.echo(
            f"error: refusing to bind {host} outside the WhyGraph image. Use "
            "`whygraph up`, or pass --dev-expose for development.",
            err=True,
        )
        sys.exit(2)

    # Lazy-imported so `--help` stays fast and doesn't load the HTTP stack.
    import uvicorn

    from whygraph.portal import db as portal_db
    from whygraph.portal.app import PortalServer, PortalStartupError, create_portal_app
    from whygraph.portal.hosts import TRUSTED_PROXIES_ENV, parse_trusted_proxies

    try:
        # Unset trusts no proxy - not uvicorn's default of 127.0.0.1.
        forwarded_allow_ips = parse_trusted_proxies(os.environ.get(TRUSTED_PROXIES_ENV))
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    from ..console import console

    try:
        url = portal_db.database_url()
    except portal_db.PortalDatabaseNotConfigured as exc:
        click.echo(
            f"error: {exc}. The portal stores its data in Postgres since 2.1; start "
            "it with 'whygraph up' (which runs the database for you), or point "
            f"{portal_db.DATABASE_URL_ENV} at a Postgres database.",
            err=True,
        )
        sys.exit(2)

    if data_dir is not None:
        os.environ[portal_db.DATA_ENV_VAR] = str(data_dir.expanduser())
    lock = _lock_data_dir(portal_db.data_dir())
    try:
        app = create_portal_app(port=port)
        console.print(f"[bold]WhyGraph portal[/] → {_banner_url(port)}")
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_config=None,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SEC,
            forwarded_allow_ips=forwarded_allow_ips,
        )
        # PortalServer ends open event streams at the start of shutdown,
        # before uvicorn's graceful wait (which would otherwise sit out
        # the whole timeout on every open stream).
        PortalServer(config, app).run()
    finally:
        lock.close()
    error = app.state.portal.startup_error
    if isinstance(error, portal_db.PortalDatabaseUnreachable):
        host = url.host or ""
        hint = (
            f"is the {host} container running? (whygraph status)"
            if host.endswith("-postgres")
            else "is the database running?"
        )
        click.echo(f"error: {error} - {hint}", err=True)
        sys.exit(EXIT_DATABASE_UNREACHABLE)
    if isinstance(error, PortalStartupError):
        click.echo(f"error: {error}", err=True)
        sys.exit(2)


def _banner_url(port: int) -> str:
    """The URL the start banner names: production's base URL, else loopback.

    The stored mode is only known once the lifespan ran, so this reads the
    environment: a production deployment sets ``WHYGRAPH_MODE`` and
    ``WHYGRAPH_BASE_URL`` (the lifespan refuses a bad base URL anyway).
    """
    from whygraph.portal.app import MODE_ENV
    from whygraph.portal.hosts import BASE_URL_ENV, BaseUrl

    if (os.environ.get(MODE_ENV) or "").strip().lower() == "production":
        try:
            return BaseUrl.parse(os.environ.get(BASE_URL_ENV, "")).origin
        except ValueError:
            pass
    return f"http://127.0.0.1:{port}"


def _lock_data_dir(data: Path):  # noqa: ANN202 -- an open file object
    """Take an exclusive advisory lock on ``<data>/portal.lock`` or exit 2.

    The runner, caches and migration lock are in-process, so two portals
    on one data dir would race each other. The lock is released when the
    returned file is closed (or the process dies).
    """
    import fcntl

    handle = open(data / "portal.lock", "a")  # noqa: SIM115 -- held for the run
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        click.echo(f"error: another WhyGraph portal is already using {data}", err=True)
        sys.exit(2)
    return handle
