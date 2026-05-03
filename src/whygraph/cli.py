from importlib.metadata import version as _pkg_version

import click


@click.group()
def main() -> None:
    """WhyGraph — rationale layer over CodeGraph."""


@main.command(name="version")
def version_cmd() -> None:
    """Print installed whygraph version."""
    click.echo(_pkg_version("whygraph"))
