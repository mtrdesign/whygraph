"""The ``whygraph init`` subcommand — DB bootstrap and agent wiring."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from whygraph import agents, assets

from ..console import fail


def _agents_epilog() -> str:
    """Render the supported-agents block for ``whygraph init --help``.

    Lists each agent's name, aliases, scope, format, and one-line
    description — the discoverability the old ``--list-agents`` command
    provided, minus its cwd-relative config path (help text isn't
    cwd-anchored; the write step still echoes the real path). Built at
    import from :data:`whygraph.agents.AGENTS`.

    The leading ``\\b`` marker tells Click's help formatter not to
    re-wrap the block, so the indented per-agent layout survives. Click
    ends a "no-rewrap" paragraph at the first blank line, so the whole
    block is kept as a single paragraph with no interior blank lines.
    """
    lines = ["\b", "Supported agents (use with --agent):"]
    for name in sorted(agents.AGENTS):
        target = agents.AGENTS[name]
        aliases = f" (aliases: {', '.join(target.aliases)})" if target.aliases else ""
        scope = "project" if target.scope == "project" else "user"
        lines.append(
            f"  {target.name}{aliases}  —  scope: {scope}, format: {target.format}"
        )
        lines.append(f"      {target.description}")
    return "\n".join(lines)


@click.command(name="init", epilog=_agents_epilog())
@click.option(
    "--agent",
    "agent_name",
    type=click.Choice(agents.known_agent_names(), case_sensitive=False),
    default=None,
    help="Wire the WhyGraph MCP server into the named LLM agent's config.",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    help=(
        "When installing assets, overwrite existing files in the agent's"
        " destination directory. Without this flag, existing files are"
        " left alone."
    ),
)
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    help=(
        "Accept all defaults without prompting. Also implied whenever"
        " stdin is not a TTY (pipes, CI, the git hooks). Writes a"
        " default whygraph.toml if none exists and never clobbers an"
        " existing one."
    ),
)
def init_cmd(
    agent_name: str | None,
    force: bool,
    yes: bool,
) -> None:
    """Initialize the WhyGraph database under ``.whygraph/whygraph.db``.

    On a terminal this runs a guided, arrow-key setup — pick the agent,
    the analyze/rationale LLMs (+ API keys), and the source-control
    provider (+ token), review a summary that masks every secret, then
    confirm. It writes a committable ``whygraph.example.toml`` (never any
    secrets) and, once confirmed, a ready-to-run ``whygraph.toml`` (with
    the secrets you entered). Every prompt is defaulted, so a bare Enter
    accepts it.

    ``--yes`` (and any non-TTY invocation: pipes, CI, the git hooks)
    skips all prompts and uses defaults — writing a default
    ``whygraph.toml`` only if none exists and never clobbering an existing
    one. A bare non-TTY ``init`` refreshes only the example, as before.

    Either way it ensures the project's ``.gitignore`` keeps the
    user-owned config and generated caches out of git (``whygraph.toml``,
    ``.whygraph/``, ``.codegraph/``).

    Does **not** index CodeGraph — that happens on ``whygraph scan``,
    which populates ``.codegraph/codegraph.db`` (and refreshes it on every
    subsequent run).

    With ``--agent X``, registers the WhyGraph MCP server with the named
    agent (run ``whygraph init --help`` for the list of supported
    agents). All supported agents are project-scoped — their MCP config
    file is written / merged in the repo. Agents whose config can't be
    written automatically get the snippet printed for manual pasting.

    If the chosen agent ships a bundled asset tree (see
    :attr:`whygraph.agents.AgentTarget.has_assets`), the tree is copied
    into the matching destination directory under the repo. Pre-existing
    files are left alone unless ``--force`` is passed.

    Idempotent — re-running on an already-initialized project just
    confirms both databases are present and at head.
    """
    project_root = Path.cwd()

    _run_preflight()

    # Prompt only on a real terminal and only when not told to accept
    # defaults. Pipes / CI / the git hooks have no TTY and fall straight
    # through to defaults — identical to the pre-interactive behaviour.
    interactive = sys.stdin.isatty() and not yes
    answers = _gather_answers(project_root, agent_name, interactive=interactive)

    db_path = _ensure_db_initialized()
    click.echo(f"Initialized WhyGraph database at {db_path}")

    _scaffold_example_config(project_root, answers)
    _maybe_write_user_config(project_root, answers, write_user=interactive or yes)
    _ensure_gitignore(project_root)

    resolved_agent = answers.agent or agent_name
    if resolved_agent is None:
        click.echo(
            "Tip: run `whygraph init --help` to see supported agents,"
            " then `whygraph init --agent <name>` to wire it up."
        )
        return

    target = agents.resolve_agent(resolved_agent)
    snippet = agents.render_snippet(target)

    if not agents.is_write_supported(target):
        _print_snippet(target, project_root, snippet)
    else:
        path = agents.write_snippet(target, project_root)
        click.echo(f"Wrote whygraph MCP entry to {path}")

    if target.has_assets:
        result = assets.install_assets(target, project_root, force=force)
        _print_install_summary(target, project_root, result, force=force)


def _run_preflight() -> None:
    """Echo the diagnostics block; ``fail`` with a clean error on missing tools.

    Imported here (not at module top) so ``--help`` doesn't pay the
    import cost.
    """
    from whygraph.cli.preflight import PreflightError, run_preflight

    try:
        run_preflight()
    except PreflightError as exc:
        fail(str(exc))


def _gather_answers(project_root: Path, agent_name: str | None, *, interactive: bool):
    """Collect the init choices — interactively, or from defaults.

    In interactive mode this runs the guided flow (agent + LLMs + scan +
    a summary panel + confirm) and returns its answers. A Ctrl-C / EOF at
    any prompt or a declined confirm raises :class:`InitAborted`, which we
    turn into a clean non-zero exit **before** any file is written or the
    DB is bootstrapped. Non-interactive callers get the defaults with the
    ``--agent`` value threaded in.

    Lazy-imports the interactive module so lightweight surfaces stay fast.
    """
    from whygraph.core.config import DEFAULT_ANSWERS, InitAnswers

    if not interactive:
        return InitAnswers(agent=agent_name, reconfigure_toml=False)

    from whygraph.cli.interactive import InitAborted, prompt_for_init

    try:
        return prompt_for_init(
            project_root,
            preset_agent=agent_name,
            on_summary=_render_summary_panel,
        )
    except InitAborted:
        fail("Aborted — no changes written.")
    # Unreachable (fail raises); satisfies type-checkers.
    return DEFAULT_ANSWERS


def _render_summary_panel(summary: str) -> None:
    """Render the pre-write review summary as a Rich panel on stderr."""
    from rich.panel import Panel

    from ..console import console

    console.print(
        Panel(summary, title="Review your choices", border_style="cyan", expand=False)
    )


def _scaffold_example_config(project_root: Path, answers) -> None:
    """Write the committable ``whygraph.example.toml`` at the project root.

    Always refreshed so it tracks the shipped defaults and any non-secret
    choices from ``answers``. Secrets are never written here. Lazy-imports
    the config helper so lightweight surfaces like ``--help`` don't pay
    the cost.
    """
    from whygraph.core.config import write_example_config

    path = write_example_config(project_root, answers)
    click.echo(
        f"Wrote example config to {path} — copy to whygraph.toml and edit to customize"
    )


def _maybe_write_user_config(project_root: Path, answers, *, write_user: bool) -> None:
    """Write ``whygraph.toml`` when appropriate, else preserve an existing one.

    Writes only when ``write_user`` (interactive or ``--yes``) **and** the
    file is absent or the user chose to reconfigure it. A bare non-TTY
    ``init`` never writes it (``write_user`` is ``False``), preserving the
    historical scaffold-only behaviour.
    """
    from whygraph.core.config import CONFIG_FILENAME, write_user_config

    user_path = project_root / CONFIG_FILENAME
    if write_user and (not user_path.exists() or answers.reconfigure_toml):
        path = write_user_config(project_root, answers)
        click.echo(f"Wrote {path} — gitignored; holds any secrets you entered")
    elif user_path.exists():
        click.echo(f"Kept existing {user_path}")


def _ensure_gitignore(project_root: Path) -> None:
    """Keep the user config and generated caches out of git.

    Idempotently adds ``whygraph.toml``, ``.whygraph/`` and ``.codegraph/``
    to the project's ``.gitignore`` (creating it if absent). Lazy-imports
    the helper to keep lightweight CLI surfaces fast.
    """
    from whygraph.core.gitignore import ensure_gitignore_entries

    added = ensure_gitignore_entries(
        project_root, ["whygraph.toml", ".whygraph/", ".codegraph/"]
    )
    if added:
        click.echo(f"Updated .gitignore (added: {', '.join(added)})")
    else:
        click.echo(".gitignore already covers WhyGraph entries")


def _ensure_db_initialized() -> Path:
    """Bootstrap the WhyGraph DB, lazy-importing the heavy chain.

    Imported here (not at module top) so that lightweight CLI surfaces
    like ``--help`` don't fail when the DB layer or its dependencies are
    mid-rewrite.
    """
    from whygraph.db import ensure_initialized

    return ensure_initialized()


def _print_snippet(
    target: agents.AgentTarget, project_root: Path, snippet: str
) -> None:
    path = agents.config_path_for(target, project_root)
    click.echo(f"Paste the following into {path}:")
    click.echo("")
    click.echo(snippet.rstrip("\n"))


def _print_install_summary(
    target: agents.AgentTarget,
    project_root: Path,
    result: assets.InstallResult,
    *,
    force: bool,
) -> None:
    """Echo a one-paragraph summary of the asset install."""
    assert target.assets_dest is not None  # guaranteed by target.has_assets
    dest = project_root.joinpath(*target.assets_dest)
    click.echo(f"Installed assets for {target.name} under {dest}/:")
    click.echo(f"  written:     {len(result.written):>3} files")
    suffix = "" if force else " (pass --force to overwrite)"
    click.echo(f"  skipped:     {len(result.skipped):>3} files{suffix}")
    click.echo(f"  overwritten: {len(result.overwritten):>3} files")
