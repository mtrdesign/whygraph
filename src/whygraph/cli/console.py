"""Cross-command terminal output for the WhyGraph CLI.

Holds the single Rich :class:`Console` instance the subcommands render
through, plus the shared :func:`fail` helper. Routing every CLI write
through one stderr-bound console keeps diagnostic output off ``stdout``,
which mirrors :mod:`whygraph.core.logger` — its :class:`RichHandler`
already targets ``Console(stderr=True)`` for the same reason — and
leaves ``stdout`` clean for structured payloads (commit IDs, JSON).
"""

from __future__ import annotations

from typing import NoReturn

import click
from rich.console import Console

console: Console = Console(stderr=True)
"""Module-level Rich :class:`Console` shared across subcommands.

Bound to ``stderr`` so panels, progress bars, and other Rich UI never
collide with structured stdout payloads — and so it composes cleanly
with the stderr-routed log handler in :mod:`whygraph.core.logger`.
"""


def fail(message: str) -> NoReturn:
    """Print ``message`` to stderr and exit with a non-zero status.

    Parameters
    ----------
    message:
        Human-readable error string. Printed verbatim via
        :func:`click.echo` to ``stderr``.

    Raises
    ------
    click.exceptions.Exit
        Always raised with code ``1``. The ``NoReturn`` annotation lets
        type-checkers see that callers don't need to handle a return
        value after invoking :func:`fail`.
    """
    click.echo(message, err=True)
    raise click.exceptions.Exit(1)
