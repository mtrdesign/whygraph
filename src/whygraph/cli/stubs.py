"""Removal stubs for the 1.x entry points dropped in 2.0.0.

``whygraph init``, ``whygraph serve`` and the ``whygraph-mcp`` console
script were replaced by the WhyGraph portal. Each still exists, so a 1.x
habit (or a 1.x agent config that runs ``whygraph-mcp``) gets a clear
message instead of "no such command": the message goes to **stderr** and
the exit status is ``2``. The same strings are baked into the host shim
and the host ``whygraph-mcp`` stub written by ``whygraph install``, so
Docker users see them too. They name ``whygraph up`` only: the native
``whygraph portal`` is dev-only in 2.0.
"""

from __future__ import annotations

import sys

import click

INIT_REMOVED = (
    "`whygraph init` was removed in 2.0.0. Start the portal with `whygraph up`"
    " and add this repo from the Projects page."
)
"""Printed by ``whygraph init``."""

SERVE_REMOVED = (
    "`whygraph serve` was removed in 2.0.0. Start the portal with `whygraph up`"
    " and add this repo from the Projects page."
)
"""Printed by ``whygraph serve`` (in the image and by the host shim)."""

MCP_REMOVED = (
    "whygraph-mcp was removed in 2.0.0. Add this repo in the WhyGraph portal"
    " (`whygraph up`); it configures your agent to connect over HTTP."
)
"""Printed by ``whygraph-mcp`` (the console script and the host stub)."""

REMOVED_EXIT_CODE = 2
"""Exit status of every removal stub."""


def removed_command(name: str, message: str) -> click.Command:
    """Build a hidden Click command that prints ``message`` and exits ``2``.

    The command accepts (and ignores) any arguments and options,
    ``--help`` included, so every 1.x invocation reaches the message.

    Parameters
    ----------
    name : str
        The subcommand name.
    message : str
        What to print to stderr.

    Returns
    -------
    click.Command
        The stub command.
    """

    @click.command(
        name=name,
        hidden=True,
        add_help_option=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    def _stub(args: tuple[str, ...]) -> None:
        click.echo(message, err=True)
        sys.exit(REMOVED_EXIT_CODE)

    return _stub


def whygraph_mcp() -> None:
    """Entry point of the ``whygraph-mcp`` console script: the removal message.

    Writes :data:`MCP_REMOVED` to stderr only (stdout stays empty, so an
    agent speaking MCP over stdio reads nothing it could misparse) and
    exits with status ``2``.
    """
    sys.stderr.write(MCP_REMOVED + "\n")
    sys.exit(REMOVED_EXIT_CODE)


__all__ = [
    "INIT_REMOVED",
    "MCP_REMOVED",
    "REMOVED_EXIT_CODE",
    "SERVE_REMOVED",
    "removed_command",
    "whygraph_mcp",
]
