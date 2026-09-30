"""The ``whygraph serve`` subcommand - removed in 2.0.0.

The Explorer and Chat are served by the WhyGraph portal (``whygraph up``).
The command stays as a stub that prints the removal message to stderr and
exits ``2``.
"""

from __future__ import annotations

from ..stubs import SERVE_REMOVED, removed_command

serve_cmd = removed_command("serve", SERVE_REMOVED)
