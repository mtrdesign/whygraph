"""WhyGraph command-line interface.

Assembles the top-level ``whygraph`` Click group and registers every
subcommand. Each subcommand lives in its own module under
:mod:`whygraph.cli.commands`; this module only wires them onto the group.
"""

from __future__ import annotations

import click

from .commands.analyze import analyze_cmd
from .commands.init import init_cmd
from .commands.scan import scan_cmd
from .commands.version import version_cmd


@click.group()
def main() -> None:
    """WhyGraph — rationale layer over CodeGraph."""
    # Logging is configured per-command so the top-level group does not
    # blow up when sibling modules (e.g. config resolution) are mid-rewrite.
    pass


main.add_command(version_cmd)
main.add_command(init_cmd)
main.add_command(scan_cmd)
main.add_command(analyze_cmd)
