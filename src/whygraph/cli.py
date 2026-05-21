from __future__ import annotations

from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, TypeVar

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from whygraph import clients
from whygraph.scan import Crawler, GitCrawler, GitHubCrawler

if TYPE_CHECKING:
    from collections.abc import Callable

    from whygraph.analyze import Description
    from whygraph.core.config import Config
    from whygraph.services.git import Commit, Repository
    from whygraph.services.github import GitHubClient

_T = TypeVar("_T")


@click.group()
def main() -> None:
    """WhyGraph — rationale layer over CodeGraph."""
    # Logging is configured per-command so the top-level group does not
    # blow up when sibling modules (e.g. config resolution) are mid-rewrite.
    pass


def _configure_logging_best_effort() -> None:
    """Configure logging if the core dependency chain is healthy.

    Failures here are tolerated so the CLI can still expose pure-CLI
    surfaces (``--help``, ``--list-clients``) while parts of the package
    are in flux.
    """
    try:
        from whygraph.core import configure_logging, get_config

        configure_logging(get_config().log_level)
    except Exception:  # noqa: BLE001 — best-effort, intentional
        pass


@main.command(name="version")
def version_cmd() -> None:
    """Print installed whygraph version."""
    click.echo(_pkg_version("whygraph"))


@main.command(name="init")
@click.option(
    "--client",
    "client_name",
    type=click.Choice(clients.known_client_names(), case_sensitive=False),
    default=None,
    help="Wire the WhyGraph MCP server into the named LLM client's config.",
)
@click.option(
    "--print",
    "print_only",
    is_flag=True,
    help="Print the MCP snippet to stdout instead of writing any config file.",
)
@click.option(
    "--list-clients",
    "list_clients",
    is_flag=True,
    help="List supported clients (with config-file paths) and exit.",
)
def init_cmd(client_name: str | None, print_only: bool, list_clients: bool) -> None:
    """Initialize the WhyGraph database under ``.whygraph/whygraph.db``.

    With ``--client X``, also register the WhyGraph MCP server with the
    named client. Project-scoped clients (Claude Code, Cursor, VS Code /
    Copilot) get their config file written/merged in the repo. User-scoped
    clients (Codex, Claude Desktop) get the snippet printed for the
    developer to paste manually.

    Idempotent — re-running on an already-initialized DB just confirms
    both schema layers are at head.
    """
    _configure_logging_best_effort()

    if list_clients:
        _print_client_list()
        return

    db_path = _ensure_db_initialized()
    click.echo(f"Initialized WhyGraph database at {db_path}")

    if client_name is None:
        click.echo(
            "Tip: run `whygraph init --list-clients` to see supported editors,"
            " then `whygraph init --client <name>` to wire it up."
        )
        return

    target = clients.resolve_client(client_name)
    project_root = Path.cwd()
    snippet = clients.render_snippet(target)

    if print_only or not clients.is_write_supported(target):
        _print_snippet(target, project_root, snippet)
    else:
        path = clients.write_snippet(target, project_root)
        click.echo(f"Wrote whygraph MCP entry to {path}")

    if target.name == "claude":
        click.echo(
            "Tip: for slash commands and skills, also install the Claude Code"
            " plugin:\n"
            "  /plugin marketplace add /absolute/path/to/whygraph\n"
            "  /plugin install whygraph@whygraph"
        )


@main.command(name="scan")
def scan_cmd() -> None:
    """Run the source crawlers, then describe each commit with the LLM."""
    _configure_logging_best_effort()

    # Lazy-imported so that --help and other lightweight CLI surfaces
    # don't fail when the DB or git layers are mid-rewrite.
    from whygraph.analyze import LlmDescriptor
    from whygraph.core import get_config
    from whygraph.db import ensure_initialized
    from whygraph.scan import AnalyzeCrawler
    from whygraph.services.git import Repository
    from whygraph.services.github import GitHubClient
    from whygraph.services.llm import LlmError

    db_path = ensure_initialized()
    repository = Repository(Path.cwd())
    github_client = GitHubClient.for_repository(repository)
    config = get_config()

    try:
        descriptor = LlmDescriptor.from_config(config.analyze)
        analyze_skip: str | None = None
    except LlmError as exc:
        descriptor = None
        analyze_skip = str(exc)

    _render_scan_panel(
        Console(),
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


@main.command(name="analyze")
@click.argument("target")
@click.argument("baseline", required=False)
def analyze_cmd(target: str, baseline: str | None) -> None:
    """Describe a commit's diff with the configured LLM.

    TARGET is the commit being analyzed. With no BASELINE it is compared
    to its parent (the previous commit in history); with a BASELINE the
    diff analyzed is ``git diff BASELINE..TARGET``.

    Every commit named on the command line must already exist in the
    WhyGraph database — run ``whygraph scan`` first. The generated
    description is printed, not persisted.
    """
    _configure_logging_best_effort()

    # Lazy-imported so lightweight CLI surfaces (--help) don't fail when
    # sibling layers are mid-rewrite — same rationale as scan_cmd.
    from whygraph.analyze import AnalyzeError, LlmDescriptor
    from whygraph.core import get_config
    from whygraph.db import ensure_initialized, get_session
    from whygraph.db.models.commit import Commit as CommitRow
    from whygraph.services.git import GitError, Repository
    from whygraph.services.llm import LlmError

    ensure_initialized()
    repo = Repository(Path.cwd())

    target_commit = _resolve_commit(repo, target)
    if target_commit is None:
        _fail(f"{target!r} is not a commit in this repository")
    baseline_commit = None
    if baseline is not None:
        baseline_commit = _resolve_commit(repo, baseline)
        if baseline_commit is None:
            _fail(f"{baseline!r} is not a commit in this repository")

    checked = [target_commit]
    if baseline_commit is not None:
        checked.append(baseline_commit)
    with get_session() as session:
        for commit in checked:
            if session.get(CommitRow, commit.sha) is None:
                _fail(
                    f"commit {commit.sha[:12]} is not in the WhyGraph database; "
                    f"run 'whygraph scan' first"
                )

    try:
        if baseline_commit is None:
            diff = repo.diff(target_commit)
        else:
            diff = repo.diff_range(baseline_commit.sha, target_commit.sha)
        descriptor = LlmDescriptor.from_config(get_config().analyze)
        description = descriptor.describe(diff)
    except (GitError, AnalyzeError, LlmError) as exc:
        _fail(str(exc))

    _echo_description(target_commit, baseline_commit, diff, description)


def _ensure_db_initialized() -> Path:
    """Bootstrap the WhyGraph DB, lazy-importing the heavy chain.

    Imported here (not at module top) so that lightweight CLI surfaces
    like ``--list-clients`` and ``--help`` don't fail when the DB layer
    or its dependencies are mid-rewrite.
    """
    from whygraph.db import ensure_initialized

    return ensure_initialized()


def _print_client_list() -> None:
    click.echo("Supported clients:")
    for name in sorted(clients.CLIENTS):
        target = clients.CLIENTS[name]
        aliases = f" (aliases: {', '.join(target.aliases)})" if target.aliases else ""
        path = clients.config_path_for(target, Path.cwd())
        scope = "project" if target.scope == "project" else "user"
        click.echo(f"  {target.name}{aliases}")
        click.echo(f"    scope: {scope}  format: {target.format}")
        click.echo(f"    path:  {path}")
        click.echo(f"    note:  {target.description}")


def _print_snippet(
    target: clients.ClientTarget, project_root: Path, snippet: str
) -> None:
    path = clients.config_path_for(target, project_root)
    click.echo(f"Paste the following into {path}:")
    click.echo("")
    click.echo(snippet.rstrip("\n"))
    if (
        target.name == "claude-desktop"
        and not clients.claude_desktop_supported_platform()
    ):
        click.echo(
            "\nNote: the path above is the macOS location. On Windows/Linux,"
            " Claude Desktop's config lives elsewhere — check its docs."
        )


def _resolve_commit(repo: "Repository", ref: str) -> "Commit | None":
    """Resolve a commit-ish to a git ``Commit``, or ``None`` if git can't.

    Accepts anything ``git log`` understands — a full or abbreviated SHA,
    a branch name, ``HEAD~1`` — and returns the resolved commit. Returns
    ``None`` for a ref git rejects, so the caller can emit a clean error.
    """
    from whygraph.services.git import Commits, GitError

    try:
        return next(iter(Commits(repo.root, ref)), None)
    except GitError:
        return None


def _echo_description(
    target: "Commit",
    baseline: "Commit | None",
    diff: str,
    description: "Description",
) -> None:
    """Print the provenance, the analyzed diff, and the model's output.

    The diff is shown with diff syntax highlighting; the model's
    description is wrapped in a panel so it stands out from the diff
    and the surrounding provenance metadata.
    """
    console = Console()
    against = "its parent" if baseline is None else baseline.sha[:12]
    in_tok = "n/a" if description.input_tokens is None else description.input_tokens
    out_tok = "n/a" if description.output_tokens is None else description.output_tokens

    click.echo(f"Analyzed {target.sha[:12]} against {against}")
    click.echo("")
    click.echo(f"provider:      {description.provider}")
    click.echo(f"model:         {description.model}")
    click.echo(f"input tokens:  {in_tok}")
    click.echo(f"output tokens: {out_tok}")
    click.echo(f"truncated:     {'yes' if description.truncated else 'no'}")
    click.echo("")

    console.rule("git diff", align="left", style="dim")
    console.print(Syntax(diff, "diff", theme="ansi_dark", word_wrap=True))
    console.print()
    console.print(
        Panel(
            Text(description.text or "(no description returned)"),
            title="LLM description",
            title_align="left",
            border_style="green",
            padding=(1, 2),
        )
    )


def _render_scan_panel(
    console: Console,
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
        rows.append(
            ("GitHub", Text("skipped — origin is not a GitHub remote", style="yellow"))
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


def _fail(message: str) -> NoReturn:
    """Print ``message`` to stderr and exit with a non-zero status."""
    click.echo(message, err=True)
    raise click.exceptions.Exit(1)
