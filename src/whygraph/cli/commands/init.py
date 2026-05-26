"""The ``whygraph init`` subcommand — DB bootstrap and agent wiring."""

from __future__ import annotations

from pathlib import Path

import click

from whygraph import agents, assets


@click.command(name="init")
@click.option(
    "--agent",
    "agent_name",
    type=click.Choice(agents.known_agent_names(), case_sensitive=False),
    default=None,
    help="Wire the WhyGraph MCP server into the named LLM agent's config.",
)
@click.option(
    "--print",
    "print_only",
    is_flag=True,
    help="Print the MCP snippet to stdout instead of writing any config file.",
)
@click.option(
    "--list-agents",
    "list_agents",
    is_flag=True,
    help="List supported agents (with config-file paths) and exit.",
)
@click.option(
    "--install-assets/--no-install-assets",
    "install_assets",
    default=True,
    help=(
        "Copy the chosen agent's bundled assets (if any) into the project."
        " Default: enabled. No-op for agents that ship no asset tree."
    ),
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
def init_cmd(
    agent_name: str | None,
    print_only: bool,
    list_agents: bool,
    install_assets: bool,
    force: bool,
) -> None:
    """Initialize the WhyGraph database under ``.whygraph/whygraph.db``.

    With ``--agent X``, also register the WhyGraph MCP server with the
    named agent. Project-scoped agents (Claude Code, Cursor, VS Code /
    Copilot) get their config file written/merged in the repo. User-scoped
    agents (Codex, Claude Desktop) get the snippet printed for the
    developer to paste manually.

    If the chosen agent ships a bundled asset tree (see
    :attr:`whygraph.agents.AgentTarget.has_assets`), the tree is copied
    into the matching destination directory under the repo. Pre-existing
    files are left alone unless ``--force`` is passed; pass
    ``--no-install-assets`` to skip the copy entirely.

    Idempotent — re-running on an already-initialized DB just confirms
    both schema layers are at head.
    """
    if list_agents:
        _print_agent_list()
        return

    db_path = _ensure_db_initialized()
    click.echo(f"Initialized WhyGraph database at {db_path}")

    if agent_name is None:
        click.echo(
            "Tip: run `whygraph init --list-agents` to see supported agents,"
            " then `whygraph init --agent <name>` to wire it up."
        )
        return

    target = agents.resolve_agent(agent_name)
    project_root = Path.cwd()
    snippet = agents.render_snippet(target)

    if print_only or not agents.is_write_supported(target):
        _print_snippet(target, project_root, snippet)
    else:
        path = agents.write_snippet(target, project_root)
        click.echo(f"Wrote whygraph MCP entry to {path}")

    if install_assets and target.has_assets:
        result = assets.install_assets(target, project_root, force=force)
        _print_install_summary(target, project_root, result, force=force)


def _ensure_db_initialized() -> Path:
    """Bootstrap the WhyGraph DB, lazy-importing the heavy chain.

    Imported here (not at module top) so that lightweight CLI surfaces
    like ``--list-agents`` and ``--help`` don't fail when the DB layer
    or its dependencies are mid-rewrite.
    """
    from whygraph.db import ensure_initialized

    return ensure_initialized()


def _print_agent_list() -> None:
    click.echo("Supported agents:")
    for name in sorted(agents.AGENTS):
        target = agents.AGENTS[name]
        aliases = f" (aliases: {', '.join(target.aliases)})" if target.aliases else ""
        path = agents.config_path_for(target, Path.cwd())
        scope = "project" if target.scope == "project" else "user"
        click.echo(f"  {target.name}{aliases}")
        click.echo(f"    scope: {scope}  format: {target.format}")
        click.echo(f"    path:  {path}")
        click.echo(f"    note:  {target.description}")


def _print_snippet(
    target: agents.AgentTarget, project_root: Path, snippet: str
) -> None:
    path = agents.config_path_for(target, project_root)
    click.echo(f"Paste the following into {path}:")
    click.echo("")
    click.echo(snippet.rstrip("\n"))
    if (
        target.name == "claude-desktop"
        and not agents.claude_desktop_supported_platform()
    ):
        click.echo(
            "\nNote: the path above is the macOS location. On Windows/Linux,"
            " Claude Desktop's config lives elsewhere — check its docs."
        )


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
