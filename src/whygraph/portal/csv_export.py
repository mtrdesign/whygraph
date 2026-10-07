"""CSV export helpers shared by the audit log and the usage ledger.

A cell a spreadsheet would read as a formula is escaped (CSV injection), and
an export that stops at its row cap says so: a final ``# truncated at N
rows`` line plus the :data:`TRUNCATED_HEADER` response header, set by the
route that knows in advance that the cap is hit.
"""

from __future__ import annotations

import csv
import io
from typing import Any

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
"""First characters a spreadsheet reads as a formula (CSV injection)."""

TRUNCATED_HEADER = "X-WhyGraph-Truncated"
"""Response header set to ``"1"`` on an export cut at its row cap."""


def csv_cell(value: Any) -> str:
    """One CSV cell, escaped against formula injection.

    A string whose first character is ``=``, ``+``, ``-``, ``@``, a tab or
    a carriage return is prefixed with ``'``; ``None`` is empty.

    Parameters
    ----------
    value : object
        The cell's value.

    Returns
    -------
    str
        The cell text (``csv.writer`` quotes it).
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    if value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_line(cells: list[Any]) -> str:
    """One CSV line (with its line break), every cell escaped by :func:`csv_cell`.

    Parameters
    ----------
    cells : list
        The line's values.

    Returns
    -------
    str
        The encoded line.
    """
    buffer = io.StringIO()
    csv.writer(buffer).writerow([csv_cell(c) for c in cells])
    return buffer.getvalue()


def truncated_line(max_rows: int) -> str:
    """The last line of an export cut at ``max_rows`` rows.

    Parameters
    ----------
    max_rows : int
        The cap that was hit.

    Returns
    -------
    str
        ``"# truncated at <max_rows> rows"`` and a line break.
    """
    return f"# truncated at {max_rows} rows\r\n"


__all__ = [
    "FORMULA_PREFIXES",
    "TRUNCATED_HEADER",
    "csv_cell",
    "csv_line",
    "truncated_line",
]
