"""The ``whygraph scan`` subcommand - removed.

Scans run from the WhyGraph portal (a child process, ``whygraph.scan.child``).
The command stays as a stub that prints the removal message to stderr and
exits ``2``.
"""

from __future__ import annotations

from ..stubs import SCAN_REMOVED, removed_command

scan_cmd = removed_command("scan", SCAN_REMOVED)
