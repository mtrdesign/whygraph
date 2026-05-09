from importlib.metadata import version as _pkg_version
from pathlib import Path

import click

from whygraph.init import run_init
from whygraph.render import run_render, run_serve
from whygraph.scan import llm_descriptions as llm_module
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
@click.option(
    "--llm-recent",
    "llm_recent",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Limit the LLM diff-description phase to the most recent N "
        "commits on the default branch. Other phases (git crawl, "
        "GitHub fetch, scoring) still cover the full history."
    ),
)
@click.option(
    "--llm-model",
    "llm_model",
    default=llm_module.DEFAULT_MODEL,
    show_default=True,
    help=(
        "Model used by the `claude` subprocess in the LLM phase. The "
        "string is also persisted to commits.llm_description_model so "
        "downstream readers know which model wrote each row."
    ),
)
def scan_cmd(
    skip_score: bool,
    skip_llm_descriptions: bool,
    anthropic_api_key: str | None,
    llm_workers: int,
    llm_recent: int | None,
    llm_model: str,
) -> None:
    """Walk the repo's history and populate the WhyGraph evidence database."""
    raise SystemExit(
        run_scan(
            skip_score=skip_score,
            skip_llm_descriptions=skip_llm_descriptions,
            anthropic_api_key=anthropic_api_key,
            llm_workers=llm_workers,
            llm_recent=llm_recent,
            llm_model=llm_model,
        )
    )


@main.command(name="render")
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output HTML path. Default: <repo_root>/.whygraph/whygraph.html",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    default=False,
    help="Open the rendered HTML in your default browser.",
)
@click.option(
    "--depth",
    "depth",
    type=click.IntRange(min=1, max=4),
    default=1,
    show_default=True,
    help=(
        "Levels of the kind hierarchy to populate with per-node details. "
        "1 = Modules only (fast first paint). "
        "2 = + Classes. 3 = + Functions/Methods. 4 = Everything. "
        "Higher-level nodes still appear in the slider; only their "
        "detail panel is gated."
    ),
)
def render_cmd(out_path: Path | None, open_browser: bool, depth: int) -> None:
    """Render a self-contained HTML viewer of the CodeGraph + WhyGraph data."""
    raise SystemExit(
        run_render(out_path=out_path, open_browser=open_browser, depth=depth)
    )


@main.command(name="serve")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind host. Defaults to localhost — do not expose externally.",
)
@click.option(
    "--port",
    type=int,
    default=8765,
    show_default=True,
    help="Bind port.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    default=False,
    help="Open the viewer in your default browser after the server starts.",
)
def serve_cmd(host: str, port: int, open_browser: bool) -> None:
    """Serve the live viewer with on-demand rationale generation."""
    raise SystemExit(
        run_serve(host=host, port=port, open_browser=open_browser)
    )
