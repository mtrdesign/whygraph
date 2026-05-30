"""The ``whygraph scan`` subcommand — run source crawlers, then analyze."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import click
from rich.panel import Panel
from rich.progress import Progress
from rich.table import Table
from rich.text import Text

from whygraph.scan import Crawler, GitCrawler, GitHubCrawler

from ..console import console

if TYPE_CHECKING:
    from collections.abc import Callable

    from whygraph.core.config import Config
    from whygraph.services.git import Repository
    from whygraph.services.github import GitHubClient

_T = TypeVar("_T")


@click.command(name="scan")
@click.option(
    "--no-llm-descriptions",
    "no_llm_descriptions",
    is_flag=True,
    default=False,
    help=(
        "Skip Phase 2 (per-commit LLM descriptions). The git and GitHub "
        "crawlers still run. The MCP tools `whygraph_evidence_for` and "
        "`whygraph_rationale_brief` lazily backfill descriptions on demand, "
        "and a later `whygraph scan` (without this flag) backfills the rest."
    ),
)
def scan_cmd(no_llm_descriptions: bool) -> None:
    """Run the source crawlers, then describe each commit with the LLM."""
    # Lazy-imported so that --help and other lightweight CLI surfaces
    # don't fail when the DB or git layers are mid-rewrite.
    from whygraph.analyze import LlmDescriptor
    from whygraph.core import get_config
    from whygraph.db import ensure_initialized
    from whygraph.scan import AnalyzeCrawler
    from whygraph.services.git import Repository
    from whygraph.services.llm import LlmError

    db_path = ensure_initialized()
    config = get_config()
    repository = Repository(Path.cwd(), origin_remote=config.scan_remote)
    github_client = _select_github_client(config.scan_provider, repository)

    if no_llm_descriptions:
        # Bypass the LlmDescriptor probe entirely so a broken `[analyze]`
        # config still lets users run a fast scan and rely on the MCP
        # tools' lazy backfill for descriptions.
        descriptor = None
        analyze_skip: str | None = "--no-llm-descriptions"
    else:
        try:
            descriptor = LlmDescriptor.from_config(config.analyze)
            analyze_skip = None
        except LlmError as exc:
            descriptor = None
            analyze_skip = str(exc)

    _render_scan_panel(
        repository=repository,
        github_client=github_client,
        config=config,
        db_path=db_path,
        analyze_skip=analyze_skip,
    )

    with Progress() as progress:
        # Phase 1 — source crawlers, run concurrently.
        phase1: list[Crawler] = [GitCrawler(progress, repository=repository)]
        if github_client is not None:
            phase1.append(GitHubCrawler(progress, client=github_client))

        # Phase 2 — the analyzer, started only once phase 1 has joined
        # (it reads the commits phase 1 persisted).
        phase2: list[Crawler] = []
        if descriptor is not None:
            phase2.append(
                AnalyzeCrawler(
                    progress,
                    repository=repository,
                    descriptor=descriptor,
                    max_workers=config.scan_max_workers,
                    large_commit_file_count=config.analyze.large_commit_file_count,
                )
            )

        for c in phase1:
            c.start()
        for c in phase1:
            c.join()
        for c in phase2:
            c.start()
        for c in phase2:
            c.join()

    crawlers = phase1 + phase2
    failed = [c for c in crawlers if c.error is not None]
    for c in failed:
        click.echo(f"crawler {c.name!r} failed: {c.error}", err=True)
    if failed:
        raise click.exceptions.Exit(1)


def _select_github_client(
    provider: str, repository: "Repository"
) -> "GitHubClient | None":
    """Resolve the GitHub client for the configured ``[scan].provider``.

    Returns ``None`` when ``provider`` is ``"off"`` (remote crawling
    disabled). For ``"github"`` and ``"auto"`` it delegates to
    :meth:`GitHubClient.for_repository`, which inspects the repository's
    remote URL and returns ``None`` if it is not a GitHub remote — so a
    misconfigured ``"github"`` or a non-GitHub ``"auto"`` both degrade to
    "no crawl" rather than erroring.

    Parameters
    ----------
    provider : str
        The validated ``[scan].provider`` value (``"off"`` / ``"github"``
        / ``"auto"``).
    repository : Repository
        The repository whose remote URL is inspected.

    Returns
    -------
    GitHubClient or None
        A configured client, or ``None`` when crawling is off or the
        remote is not a recognized GitHub URL.
    """
    if provider == "off":
        return None
    from whygraph.services.github import GitHubClient

    return GitHubClient.for_repository(repository)


def _render_scan_panel(
    *,
    repository: "Repository",
    github_client: "GitHubClient | None",
    config: "Config",
    db_path: Path,
    analyze_skip: str | None,
) -> None:
    """Print a summary panel of what the upcoming scan will collect.

    Counts are gathered best-effort: a metric that fails (git missing,
    ``gh`` unauthenticated) renders as ``"unavailable"`` rather than
    aborting the command — the crawlers themselves surface the real
    error. The collections counted here are the same ``cached_property``
    instances the crawlers reuse, so the counts cost nothing extra once
    the crawl starts.
    """
    with console.status("Inspecting repository…", spinner="dots"):
        branch = _best_effort(lambda: repository.current_branch)
        git_count = _best_effort(lambda: len(repository.commits))
        pr_count = issue_count = None
        if github_client is not None:
            pr_count = _best_effort(lambda: len(github_client.pull_requests))
            issue_count = _best_effort(lambda: len(github_client.issues))

    if github_client is not None:
        repo_label = f"{github_client.owner}/{github_client.name}"
    else:
        repo_label = repository.root.name or str(repository.root)

    rows: list[tuple[str, object]] = [
        ("Repository", repo_label),
        ("Branch", str(branch) if branch is not None else "unknown"),
        ("Database", str(db_path)),
        ("", ""),
        ("Git commits", str(git_count) if git_count is not None else "unavailable"),
    ]
    if github_client is None:
        rows.append(("GitHub", Text(_github_skip_reason(config), style="yellow")))
    else:
        rows.append(
            ("Pull requests", str(pr_count) if pr_count is not None else "unavailable")
        )
        rows.append(
            ("Issues", str(issue_count) if issue_count is not None else "unavailable")
        )

    rows.append(("", ""))
    if analyze_skip is None:
        rows.append(("LLM descriptions", _analyze_model_label(config)))
    else:
        rows.append(
            ("LLM descriptions", Text(f"skipped — {analyze_skip}", style="yellow"))
        )
    rows.append(("Worker threads", str(config.scan_max_workers)))

    grid = Table.grid(padding=(0, 3))
    grid.add_column(style="bold cyan", justify="right", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(label, value)

    console.print(
        Panel(
            grid,
            title="whygraph scan",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()


def _github_skip_reason(config: "Config") -> str:
    """Explain why the GitHub crawl is being skipped, per ``[scan].provider``.

    Called only when no GitHub client was resolved. ``"off"`` means the
    user disabled remote crawling; otherwise the configured remote did
    not resolve to a GitHub URL.
    """
    provider = config.scan_provider
    if provider == "off":
        return "skipped — source control disabled ([scan].provider = off)"
    if provider == "auto":
        return f"skipped — {config.scan_remote!r} remote is not a recognized remote"
    return f"skipped — {config.scan_remote!r} remote is not a GitHub remote"


def _best_effort(fn: "Callable[[], _T]") -> "_T | None":
    """Run ``fn``; return its result, or ``None`` if it raised.

    Scan-panel metrics are gathered through this wrapper so a missing
    ``gh`` CLI or an unreadable git repository degrades a single panel
    row to ``"unavailable"`` instead of aborting the command before the
    crawlers — which do the real error reporting — get to run.
    """
    try:
        return fn()
    except Exception:  # noqa: BLE001 — best-effort panel metric, intentional
        return None


def _analyze_model_label(config: "Config") -> str:
    """Return the ``provider · model`` the analyze crawler will use.

    When ``[analyze].model`` is unset the descriptor defers to the
    provider's own ``[llm.<provider>]`` model; this resolves that same
    fallback so the panel reports the model that will actually run.
    """
    provider = config.analyze.provider
    model = config.analyze.model
    if model is None:
        section = getattr(config.llm, provider.replace("-", "_"), None)
        model = getattr(section, "model", None)
    return f"{provider} · {model}" if model else provider
