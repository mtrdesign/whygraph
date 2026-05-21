"""The ``whygraph version`` subcommand."""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

import click


@click.command(name="version")
def version_cmd() -> None:
    """Print installed whygraph version."""
    click.echo(_pkg_version("whygraph"))
