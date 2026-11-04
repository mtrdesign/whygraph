"""The hidden ``whygraph release-check`` subcommand.

Used by the host shim's ``update`` verb: it asks GitHub Releases which
version to move to and prints four ``key=value`` lines for ``sh`` to read.
"""

from __future__ import annotations

import sys

import click
import httpx

from whygraph.core.releases import (
    ReleaseCheckError,
    build_client,
    fetch_releases,
    format_version,
    parse_version,
    resolve,
    running_version,
)

TRANSPORT: httpx.BaseTransport | None = None
"""Test seam: a transport to use instead of the network."""


@click.command(name="release-check", hidden=True)
@click.option("--target", default=None, help="An explicit X.Y.Z to resolve.")
def release_check_cmd(target: str | None) -> None:
    """Resolve the update target and print status, target, manual, verified.

    Parameters
    ----------
    target : str, optional
        An explicit ``X.Y.Z`` version; the newest release when omitted.

    Notes
    -----
    Exit 0 with exactly four stdout lines (``status=``, ``target=``,
    ``manual=``, ``verified=``); exit 1 with one stderr line when GitHub
    cannot be reached; exit 2 for a malformed ``--target``.
    """
    parsed_target = None
    if target is not None:
        parsed_target = parse_version(target)
        if parsed_target is None:
            click.echo(f"invalid --target {target!r}: expected X.Y.Z", err=True)
            sys.exit(2)
    current = running_version()
    try:
        with build_client("whygraph-cli", TRANSPORT) as client:
            releases = fetch_releases(client)
    except ReleaseCheckError as exc:
        click.echo(f"could not reach GitHub Releases ({exc.kind})", err=True)
        sys.exit(1)
    result = resolve(releases, current, target=parsed_target)
    chosen = format_version(result.target.version) if result.target else ""
    click.echo(f"status={result.status}")
    click.echo(f"target={chosen}")
    click.echo(f"manual={int(result.manual)}")
    click.echo(f"verified={int(result.verified)}")
