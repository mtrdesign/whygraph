"""The ``whygraph analyze`` subcommand — describe a commit's diff."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from .._shared import _configure_logging_best_effort

if TYPE_CHECKING:
    from whygraph.analyze import Description
    from whygraph.services.git import Commit, Repository


@click.command(name="analyze")
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


def _fail(message: str) -> NoReturn:
    """Print ``message`` to stderr and exit with a non-zero status."""
    click.echo(message, err=True)
    raise click.exceptions.Exit(1)
