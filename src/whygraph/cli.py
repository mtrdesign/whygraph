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
@click.option(
    "--no-llm-description",
    "skip_llm_descriptions",
    is_flag=True,
    default=False,
    help="Skip the per-commit LLM diff description phase.",
)
@click.option(
    "--anthropic-key",
    "anthropic_api_key",
    default=None,
    help=(
        "Anthropic API key for the LLM phase. If set, the `claude` "
        "subprocess uses API billing with this key. If omitted, the "
        "subprocess inherits a stripped env (no ANTHROPIC_API_KEY), "
        "which forces Claude.ai subscription billing."
    ),
)
@click.option(
    "--llm-workers",
    "llm_workers",
    type=click.IntRange(min=1),
    default=4,
    show_default=True,
    help="Parallel `claude` subprocesses in the LLM phase.",
)
def scan_cmd(
    skip_score: bool,
    skip_llm_descriptions: bool,
    anthropic_api_key: str | None,
    llm_workers: int,
) -> None:
    """Walk the repo's history and populate the WhyGraph evidence database."""
    raise SystemExit(
        run_scan(
            skip_score=skip_score,
            skip_llm_descriptions=skip_llm_descriptions,
            anthropic_api_key=anthropic_api_key,
            llm_workers=llm_workers,
        )
    )
