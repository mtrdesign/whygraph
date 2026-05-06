from importlib.metadata import version as _pkg_version

import click

from whygraph.init import run_init
from whygraph.scan.runner import run_scan


@click.group()
def main() -> None:
    """WhyGraph — rationale layer over CodeGraph."""


@main.command(name="version")
def version_cmd() -> None:
    """Print installed whygraph version."""
    click.echo(_pkg_version("whygraph"))


@main.command(name="init")
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt when bootstrapping nvm.",
)
def init_cmd(assume_yes: bool) -> None:
    """Bootstrap CodeGraph in the current repository."""
    raise SystemExit(run_init(assume_yes=assume_yes))


@main.command(name="scan")
@click.option(
    "--no-score",
    "skip_score",
    is_flag=True,
    default=False,
    help="Skip TF-IDF scoring after data collection.",
)
def scan_cmd(skip_score: bool) -> None:
    """Walk the repo's history and populate the WhyGraph evidence database."""
    raise SystemExit(run_scan(skip_score=skip_score))
