"""The ``whygraph init`` subcommand - removed in 2.0.0.

Initialization moved to the WhyGraph portal (add the repo on the Projects
page, then Initialize). The command stays as a stub that prints the
removal message to stderr and exits ``2``.
"""

from __future__ import annotations

from ..stubs import INIT_REMOVED, removed_command

init_cmd = removed_command("init", INIT_REMOVED)
