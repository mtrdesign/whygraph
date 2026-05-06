"""Parallel crawler orchestration for `whygraph scan`."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from whygraph.scan import db as db_module
from whygraph.scan import git as git_module
from whygraph.scan import github as github_module
from whygraph.scan import llm_descriptions as llm_module
from whygraph.scan import scoring as scoring_module


def run_scan(
    repo_root: Path | None = None,
    *,
    skip_score: bool = False,
    skip_llm_descriptions: bool = False,
    anthropic_api_key: str | None = None,
    llm_workers: int = llm_module.DEFAULT_MAX_WORKERS,
) -> int:
    cwd = repo_root if repo_root is not None else Path.cwd()
    try:
        root = git_module.repo_root(cwd)
    except git_module.GitError as exc:
        click.echo(f"Not a git repository: {exc}", err=True)
        return 1

    db_path = db_module.default_db_path(root)
    # Apply migrations + enable WAL once on the main thread before crawler
    # threads open their own connections — avoids a DDL race.
    with db_module.Database(db_path):
        pass

    branch = git_module.default_branch(root)
    shas = list(git_module.walk_first_parent(root, branch))

    llm_config = llm_module.LlmConfig(
        anthropic_api_key=anthropic_api_key,
        max_workers=llm_workers,
    )

    console = Console()
    console.print(
        _build_overview_panel(
            root=root,
            branch=branch,
            commit_count=len(shas),
            db_path=db_path,
            skip_score=skip_score,
            skip_llm_descriptions=skip_llm_descriptions,
            llm_config=llm_config,
        )
    )

    rc = 0
    summaries: dict[str, str] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        git_task = progress.add_task("git   ", total=len(shas) or 1)
        github_task = progress.add_task("github", total=None)
        score_task = progress.add_task("score ", total=None, start=False)
        llm_task = progress.add_task("llm   ", total=None, start=False)

        with ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="whygraph-crawler"
        ) as ex:
            futures: dict[str, Future[str]] = {
                "git": ex.submit(
                    _git_crawler, root, branch, shas, db_path, progress, git_task
                ),
                "github": ex.submit(
                    _github_crawler, root, db_path, progress, github_task
                ),
            }
            for label, fut in futures.items():
                try:
                    summaries[label] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    summaries[label] = f"failed: {exc}"
                    rc = 1

        if skip_score:
            progress.update(
                score_task, total=1, completed=1, description="score (skipped)"
            )
            summaries["score"] = "skipped (--no-score)"
        elif rc == 0:
            progress.start_task(score_task)
            try:
                summaries["score"] = scoring_module.run_scoring_phase(
                    db_path, progress, score_task
                )
            except Exception as exc:  # noqa: BLE001
                summaries["score"] = f"failed: {exc}"
                rc = 1
        else:
            progress.update(
                score_task, total=1, completed=1, description="score (skipped)"
            )
            summaries["score"] = "skipped (crawler failure)"

        if skip_llm_descriptions:
            progress.update(
                llm_task, total=1, completed=1, description="llm (skipped)"
            )
            summaries["llm"] = "skipped (--no-llm-description)"
        elif rc == 0:
            if not llm_module.claude_cli_available():
                progress.update(
                    llm_task, total=1, completed=1, description="llm (skipped)"
                )
                summaries["llm"] = "skipped (claude CLI not installed)"
            else:
                progress.start_task(llm_task)
                try:
                    summaries["llm"] = llm_module.run_phase(
                        db_path,
                        root,
                        branch,
                        llm_config,
                        progress,
                        llm_task,
                    )
                except Exception as exc:  # noqa: BLE001
                    summaries["llm"] = f"failed: {exc}"
                    rc = 1
        else:
            progress.update(
                llm_task, total=1, completed=1, description="llm (skipped)"
            )
            summaries["llm"] = "skipped (prior phase failed)"

    for label, summary in summaries.items():
        if summary.startswith("failed:"):
            console.print(f"[red]\\[{label}] {summary}[/red]")
        else:
            console.print(f"\\[{label}] {summary}")

    if rc == 0:
        console.print(f"Done. Database at [bold]{db_path}[/bold]")
    return rc


def _build_overview_panel(
    *,
    root: Path,
    branch: str,
    commit_count: int,
    db_path: Path,
    skip_score: bool,
    skip_llm_descriptions: bool,
    llm_config: llm_module.LlmConfig,
) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", justify="right")
    grid.add_column()
    grid.add_row("Repo", str(root))
    grid.add_row("Branch", f"{branch} ([bold]{commit_count}[/bold] commits)")
    grid.add_row("DB", str(db_path))
    if skip_score:
        grid.add_row("Score", "[yellow]skipped[/yellow] (--no-score)")
    else:
        grid.add_row("Score", "TF-IDF")
    if skip_llm_descriptions:
        grid.add_row("LLM", "[yellow]skipped[/yellow] (--no-llm-description)")
    elif not llm_module.claude_cli_available():
        grid.add_row("LLM", "[yellow]skipped[/yellow] (claude CLI not installed)")
    else:
        billing = "API key" if llm_config.anthropic_api_key else "subscription"
        grid.add_row(
            "LLM",
            f"[bold]{llm_config.model}[/bold] · {billing} · "
            f"{llm_config.max_workers} workers",
        )
    return Panel(grid, title="whygraph scan", title_align="left", border_style="cyan")


def _git_crawler(
    repo_root: Path,
    branch: str,
    shas: list[str],
    db_path: Path,
    progress: Progress,
    task_id: TaskID,
) -> str:
    total = len(shas)
    if total == 0:
        progress.update(task_id, total=1, completed=1)
        return "no commits on default branch"
    inserted = skipped = 0
    last_sha: str | None = None
    with db_module.Database(db_path) as db:
        for sha in shas:
            if db.commit_exists(sha):
                skipped += 1
            else:
                commit = git_module.get_commit(repo_root, sha)
                db.upsert_commit(commit)
                inserted += 1
            last_sha = sha
            progress.advance(task_id)
        if last_sha is not None:
            db.set_scan_state("last_walked_sha", last_sha)
    return f"{inserted} inserted, {skipped} already present ({total} total on {branch})"


def _github_crawler(
    repo_root: Path,
    db_path: Path,
    progress: Progress,
    task_id: TaskID,
) -> str:
    detected = github_module.detect_repo(repo_root)
    if detected is None:
        progress.update(task_id, total=1, completed=1, description="github (skipped)")
        return "skipped (origin is not a GitHub remote)"
    owner, name = detected
    try:
        github_module.check_auth()
    except github_module.GitHubError as exc:
        progress.update(task_id, total=1, completed=1, description="github (skipped)")
        return f"skipped ({exc})"

    progress.update(task_id, description=f"github {owner}/{name} (PRs)")

    def _on_pr_page(fetched: int) -> None:
        progress.update(task_id, completed=fetched, total=fetched + 100)

    prs = github_module.list_pull_requests(owner, name, on_page=_on_pr_page)

    progress.update(
        task_id,
        description=f"github {owner}/{name} (issues)",
        total=None,
        completed=0,
    )

    def _on_issue_page(fetched: int) -> None:
        progress.update(task_id, completed=fetched, total=fetched + 100)

    issues = github_module.list_issues(owner, name, on_page=_on_issue_page)

    total_rows = len(prs) + len(issues)
    progress.update(
        task_id,
        description=f"github {owner}/{name} (saving)",
        total=total_rows or 1,
        completed=0,
    )
    pr_inserted = pr_updated = 0
    issue_inserted = issue_updated = 0
    link_count = 0
    with db_module.Database(db_path) as db:
        for pr in prs:
            if db.upsert_pull_request(pr):
                pr_inserted += 1
            else:
                pr_updated += 1
            db.set_pr_closing_issues(pr.number, pr.closing_issue_numbers)
            link_count += len(pr.closing_issue_numbers)
            progress.advance(task_id)
        for issue in issues:
            if db.upsert_issue(issue):
                issue_inserted += 1
            else:
                issue_updated += 1
            progress.advance(task_id)
    if total_rows == 0:
        progress.update(task_id, completed=1)
    return (
        f"PRs: {pr_inserted} inserted, {pr_updated} refreshed | "
        f"issues: {issue_inserted} inserted, {issue_updated} refreshed | "
        f"links: {link_count}"
    )
