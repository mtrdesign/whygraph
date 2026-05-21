"""The ``whygraph init`` subcommand — DB bootstrap and MCP client wiring."""

from __future__ import annotations

from pathlib import Path

import click

from whygraph import clients

from .._shared import _configure_logging_best_effort


@click.command(name="init")
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
