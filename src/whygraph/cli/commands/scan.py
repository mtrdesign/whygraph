"""The ``whygraph scan`` subcommand — run source crawlers, then analyze."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import click
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from whygraph.scan import (
    CodeGraphCrawler,
    Crawler,
    GitCrawler,
    GitHubCrawler,
    PROriginEnricher,
)

from ..console import console

if TYPE_CHECKING:
    from collections.abc import Callable

    from whygraph.core.config import Config
    from whygraph.services.git import Repository
    from whygraph.services.github import GitHubClient

_T = TypeVar("_T")

# Per-phase icons for the live headers and the closing results panel.
# Kept in one place so the whole set is trivially swappable (plan §10.5).
_ICON_STRUCTURAL = "🔎"
_ICON_PR_ORIGINS = "🔗"
_ICON_LLM = "🧠"
_ICON_CODEGRAPH = "🕸"


@click.command(name="scan")
@click.option(
    "--skip-analyze",
    "skip_analyze",
    is_flag=True,
    default=False,
    help=(
        "Skip the final LLM-descriptions phase (per-commit descriptions). "
        "The git and GitHub crawlers still run. The MCP tools "
        "`whygraph_evidence_for` and "
        "`whygraph_rationale_brief` lazily backfill descriptions on demand, "
        "and a later `whygraph scan` (without this flag) backfills the rest."
    ),
)
@click.option(
    "--codegraph/--no-codegraph",
    "refresh_codegraph",
    default=True,
    help=(
        "Refresh the CodeGraph index concurrently with the crawl — "
        "`codegraph sync` when an index exists, `codegraph init -i` on first "
        "run. Uses the local `codegraph` binary if present, else runs it "
        "inside the WhyGraph Docker image. The crawl itself doesn't need "
        "CodeGraph (only the MCP rationale/evidence tools do), so a failure "
        "here warns rather than aborting. Default: on."
    ),
)
@click.option(
    "--codegraph-image",
    "codegraph_image",
    default=None,
    help=(
        "Override the Docker image used for the CodeGraph refresh fallback "
        "(defaults to the pinned ghcr.io/mtrdesign/whygraph tag; ignored "
        "when a local `codegraph` binary is found)."
    ),
)
@click.option(
    "--remote/--no-remote",
    "remote",
    default=True,
    help=(
        "Crawl the source-control remote (GitHub PRs / issues) per "
        "`[scan].provider`. `--no-remote` skips it for a fast, offline, "
        "token-free scan — git history + CodeGraph only. Used by the "
        "auto-rescan git hooks (`whygraph hooks install`). Default: on."
    ),
)
@click.option(
    "--pr-origins/--no-pr-origins",
    "enrich_pr_origins",
    default=True,
    help=(
        "Recover a squash-merged PR's original feature-branch commits — "
        "one targeted `git fetch` of the gated PRs' heads, persisted as "
        "`commit` rows flagged off the default-branch walk so they enrich "
        "evidence without polluting area-history / refactor-walk. Needs "
        "the network, so it always runs in the remote phase and is skipped "
        "under `--no-remote`. Default: on."
    ),
)
def scan_cmd(
    skip_analyze: bool,
    refresh_codegraph: bool,
    codegraph_image: str | None,
    remote: bool,
    enrich_pr_origins: bool,
) -> None:
    """Run the source crawlers, then describe each commit with the LLM."""
    # Lazy-imported so that --help and other lightweight CLI surfaces
    # don't fail when the DB or git layers are mid-rewrite.
    from whygraph.analyze import LlmDescriptor
    from whygraph.core import get_config
    from whygraph.core.logger import scan_log_redirect
    from whygraph.db import ensure_initialized
    from whygraph.scan import AnalyzeCrawler
    from whygraph.services.git import Repository
    from whygraph.services.llm import LlmError

    db_path = ensure_initialized()
    config = get_config()
    repository = Repository(Path.cwd(), origin_remote=config.scan_remote)
    if remote:
        _apply_github_token(config)
        github_client = _select_github_client(config.scan_provider, repository)
    else:
        github_client = None

    if skip_analyze:
        # Bypass the LlmDescriptor probe entirely so a broken `[analyze]`
        # config still lets users run a fast scan and rely on the MCP
        # tools' lazy backfill for descriptions.
        descriptor = None
        analyze_skip: str | None = "--skip-analyze"
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
        codegraph_enabled=refresh_codegraph,
        remote_enabled=remote,
        pr_origins_enabled=enrich_pr_origins and github_client is not None,
    )

    # Which optional phases have work — decided up front so the phase
    # headers can be numbered against the count of phases that actually run.
    run_pr_origins = enrich_pr_origins and github_client is not None
    run_analyze = descriptor is not None
    phase_total = 1 + int(run_pr_origins) + int(run_analyze)  # Phase 1 always runs

    scan_log_path = db_path.parent / "scan.log"
    phase_timings: dict[str, float] = {}
    ran: list[Crawler] = []
    scan_t0 = time.monotonic()
    # Share the stderr `console` with Progress so the phase headers render
    # above the live bars on one stream, and add an M-of-N + elapsed column
    # so the slow LLM phase reports "12/45 · 0:00:31".
    with (
        scan_log_redirect(scan_log_path),
        Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress,
    ):
        # CodeGraph refresh — a background crawler. It writes .codegraph/
        # and has no data dependency on the WhyGraph DB, so it overlaps the
        # entire crawl (started before Phase 1, joined last). Best-effort:
        # failures land on .warning, not .error.
        codegraph_crawler = (
            CodeGraphCrawler(
                progress, project_root=repository.root, image=codegraph_image
            )
            if refresh_codegraph
            else None
        )
        if codegraph_crawler is not None:
            codegraph_crawler.start()

        n = 0

        # ── Phase 1 · Structural crawl — git + GitHub, concurrent. ──
        n += 1
        console.rule(
            f"{_ICON_STRUCTURAL} Phase {n}/{phase_total} · Structural crawl",
            style="cyan",
        )
        t0 = time.monotonic()
        phase1: list[Crawler] = [GitCrawler(progress, repository=repository)]
        if github_client is not None:
            phase1.append(GitHubCrawler(progress, client=github_client))
        ran += phase1
        for c in phase1:
            c.start()
        for c in phase1:
            c.join()
        phase_timings["Structural crawl"] = time.monotonic() - t0
        _print_phase_done(
            "Structural crawl",
            phase_timings["Structural crawl"],
            ok=all(c.error is None for c in phase1),
        )

        # ── Phase 2 · PR-origin recovery — needs Phase 1's git + PR rows.
        # Gated on a resolved client, which is None under --no-remote. ──
        if run_pr_origins:
            n += 1
            console.rule(
                f"{_ICON_PR_ORIGINS} Phase {n}/{phase_total} · PR-origin recovery",
                style="cyan",
            )
            t0 = time.monotonic()
            enricher = PROriginEnricher(
                progress,
                repository=repository,
                min_commits=config.analyze.pr_origin_min_commits,
                large_commit_file_count=config.analyze.large_commit_file_count,
            )
            ran.append(enricher)
            enricher.start()
            enricher.join()
            phase_timings["PR-origin recovery"] = time.monotonic() - t0
            _print_phase_done(
                "PR-origin recovery",
                phase_timings["PR-origin recovery"],
                ok=enricher.error is None,
            )

        # ── Phase 3 · LLM descriptions — the slow, token-heavy long pole,
        # run strictly last and alone. Only ever describes main-walk
        # commits, so the recovered on_default_branch=0 rows stay lazy. ──
        if run_analyze:
            n += 1
            console.rule(
                f"{_ICON_LLM} Phase {n}/{phase_total} · LLM descriptions",
                style="cyan",
            )
            t0 = time.monotonic()
            analyzer = AnalyzeCrawler(
                progress,
                repository=repository,
                descriptor=descriptor,
                max_workers=config.scan_max_workers,
                large_commit_file_count=config.analyze.large_commit_file_count,
            )
            ran.append(analyzer)
            analyzer.start()
            analyzer.join()
            phase_timings["LLM descriptions"] = time.monotonic() - t0
            _print_phase_done(
                "LLM descriptions",
                phase_timings["LLM descriptions"],
                ok=analyzer.error is None,
            )

        if codegraph_crawler is not None:
            codegraph_crawler.join()

    total_elapsed = time.monotonic() - scan_t0
    _render_results_panel(
        ran=ran,
        codegraph_crawler=codegraph_crawler,
        db_path=db_path,
        scan_log_path=scan_log_path,
        phase_timings=phase_timings,
        total_elapsed=total_elapsed,
    )

    crawlers = list(ran)
    if codegraph_crawler is not None:
        crawlers.append(codegraph_crawler)
    failed = [c for c in crawlers if c.error is not None]
    for c in failed:
        click.echo(f"crawler {c.name!r} failed: {c.error}", err=True)
    if failed:
        raise click.exceptions.Exit(1)


def _fmt_elapsed(seconds: float) -> str:
    """Format an elapsed duration as ``"4.1s"`` or ``"2m 08s"``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s"


def _print_phase_done(title: str, seconds: float, *, ok: bool) -> None:
    """Print the dim per-phase completion line under that phase's bars.

    ``ok`` reflects whether every crawler in the phase finished without an
    error; a failed phase gets a red ``✗`` (the real error is also surfaced
    by the closing failure sweep and results panel).
    """
    glyph = "✓" if ok else "✗"
    console.print(
        f"  {glyph} {title} · {_fmt_elapsed(seconds)}",
        style="dim" if ok else "red",
    )


def _status_glyph(*, ok: bool, warn: bool = False) -> Text:
    """Return the results-panel status cell: ``✓`` / ``⚠`` / ``✗``."""
    if not ok:
        return Text("✗", style="red")
    if warn:
        return Text("⚠", style="yellow")
    return Text("✓", style="green")


def _optional_phase_cells(
    crawler: "Crawler | None", timing: str
) -> tuple[object, object, str]:
    """Return the (status, summary, timing) cells for an optional phase.

    A crawler that never ran (phase skipped via ``--no-remote`` /
    ``--skip-analyze``) renders a dim ``— skipped`` status with no
    summary or timing.
    """
    if crawler is None:
        return Text("— skipped", style="dim"), "", ""
    return _status_glyph(ok=crawler.error is None), crawler.summary or "—", timing


def _render_results_panel(
    *,
    ran: "list[Crawler]",
    codegraph_crawler: "Crawler | None",
    db_path: Path,
    scan_log_path: Path,
    phase_timings: "dict[str, float]",
    total_elapsed: float,
) -> None:
    """Print the closing results panel — a bookend to the pre-scan panel.

    One row per phase (git + GitHub merge into the structural row), each
    carrying a status glyph, the crawler's own one-line summary, and the
    phase's elapsed time; then the database / scan-log paths. CodeGraph is
    a background task, so it gets a row but no per-phase timing. Pure
    formatting over data already in hand — no DB or network access — so it
    can never turn a successful crawl into a crash.
    """
    by_name = {c.name: c for c in ran}
    git = by_name.get("git")
    github = by_name.get("github")
    enricher = by_name.get("pr-origins")
    analyzer = by_name.get("analyze")

    def _timing(title: str) -> str:
        seconds = phase_timings.get(title)
        return _fmt_elapsed(seconds) if seconds is not None else ""

    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)  # icon
    grid.add_column(style="bold cyan", no_wrap=True)  # label
    grid.add_column(justify="center", no_wrap=True)  # status
    grid.add_column(overflow="fold")  # summary
    grid.add_column(justify="right", no_wrap=True)  # timing

    # Structural row — git + GitHub combined into one phase row.
    structural = [c for c in (git, github) if c is not None]
    structural_summary = " · ".join(c.summary for c in structural if c.summary) or "—"
    grid.add_row(
        _ICON_STRUCTURAL,
        "Structural crawl",
        _status_glyph(ok=all(c.error is None for c in structural)),
        structural_summary,
        _timing("Structural crawl"),
    )
    grid.add_row(
        _ICON_PR_ORIGINS,
        "PR-origin recovery",
        *_optional_phase_cells(enricher, _timing("PR-origin recovery")),
    )
    grid.add_row(
        _ICON_LLM,
        "LLM descriptions",
        *_optional_phase_cells(analyzer, _timing("LLM descriptions")),
    )

    # CodeGraph — background task; no per-phase timing.
    if codegraph_crawler is None:
        grid.add_row(
            _ICON_CODEGRAPH, "CodeGraph", Text("— skipped", style="dim"), "", ""
        )
    else:
        warning = getattr(codegraph_crawler, "warning", None)
        grid.add_row(
            _ICON_CODEGRAPH,
            "CodeGraph",
            _status_glyph(ok=codegraph_crawler.error is None, warn=warning is not None),
            codegraph_crawler.summary or warning or "—",
            "",
        )

    grid.add_row("", "", "", "", "")
    grid.add_row("", "Database", "", str(db_path), "")
    grid.add_row("", "Scan log", "", str(scan_log_path), "")

    console.print(
        Panel(
            grid,
            title=f"whygraph scan · done in {_fmt_elapsed(total_elapsed)}",
            title_align="left",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()


def _apply_github_token(config: "Config") -> None:
    """Export the configured GitHub token so every ``gh`` subprocess sees it.

    Resolves the token from ``[scan].token`` first, then the ambient
    ``GH_TOKEN`` / ``GITHUB_TOKEN``. When one is found and ``GH_TOKEN`` is
    not already set, it is written into ``os.environ`` so the GraphQL
    pager, :meth:`GitHubClient.check_auth`, and any preflight ``gh`` probe
    all authenticate uniformly — ``gh`` reads ``GH_TOKEN`` natively and
    child processes inherit it.

    A no-op when ``[scan].provider`` is ``"off"`` (no remote crawl). Each
    scan runs as a fresh process per project, so mutating the environment
    here cannot leak one project's token into another.

    Parameters
    ----------
    config : Config
        The loaded configuration; ``scan_token`` and ``scan_provider`` are
        consulted.
    """
    if config.scan_provider == "off":
        return
    token = (
        config.scan_token
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if token and not os.environ.get("GH_TOKEN"):
        os.environ["GH_TOKEN"] = token


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
    codegraph_enabled: bool,
    remote_enabled: bool,
    pr_origins_enabled: bool,
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
        (
            "CodeGraph",
            "refresh index"
            if codegraph_enabled
            else Text("skipped — --no-codegraph", style="yellow"),
        ),
        ("", ""),
        ("Git commits", str(git_count) if git_count is not None else "unavailable"),
    ]
    if github_client is None:
        rows.append(
            (
                "GitHub",
                Text(_github_skip_reason(config, remote_enabled), style="yellow"),
            )
        )
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
    rows.append(
        (
            "PR commit recovery",
            "recover squash-merged PR commits"
            if pr_origins_enabled
            else Text("skipped", style="yellow"),
        )
    )

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


def _github_skip_reason(config: "Config", remote_enabled: bool = True) -> str:
    """Explain why the GitHub crawl is being skipped.

    Called only when no GitHub client was resolved. ``--no-remote`` takes
    precedence (the crawl was disabled for this run); otherwise the reason
    comes from ``[scan].provider``: ``"off"`` means the user disabled
    remote crawling, and ``"github"`` / ``"auto"`` mean the configured
    remote did not resolve to a GitHub URL.
    """
    if not remote_enabled:
        return "skipped — --no-remote"
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
