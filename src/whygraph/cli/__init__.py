"""WhyGraph command-line interface.

Assembles the top-level ``whygraph`` Click group and registers every
subcommand. Each subcommand lives in its own module under
:mod:`whygraph.cli.commands`; this module only wires them onto the group
and configures logging once for the whole invocation — mirroring how
:mod:`whygraph.mcp.server` sets up logging at its own entry point.
"""

from __future__ import annotations

import click

from whygraph.core import configure_logging, get_config

from .commands.analyze import analyze_cmd
from .commands.init import init_cmd
from .commands.scan import scan_cmd
from .commands.version import version_cmd


@click.group()
def main() -> None:
    """WhyGraph — rationale layer over CodeGraph."""
    cfg = get_config()
    configure_logging(cfg.log_level, file_config=cfg.logging)


main.add_command(version_cmd)
main.add_command(init_cmd)
main.add_command(scan_cmd)
main.add_command(analyze_cmd)
