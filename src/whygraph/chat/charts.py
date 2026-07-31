"""Validate a chart directive **against the result it describes**.

The model never transcribes a value into a chart. It names *columns* of an
aggregate it has already computed, and this module resolves those names to
indices against the producer's own ``columns`` / ``rows``. A 30-bucket series
would otherwise mean 60 numbers retyped, and one fabricated point is invisible
in a rendered chart.

That makes column names the failure mode (the dominant one reported for
spec-generating chart agents), so **every refusal names both the offending value
and what was available** — the message is the defense, not decoration, because a
tool error is something the model can read and correct inside the same turn.

This module knows nothing about SQL and nothing about ECharts. Any producer that
returns ``{columns, rows}`` can be charted, and any renderer can consume the
result — which is what makes a third producer a one-line addition.

Notes
-----
**Stacked kinds take long format, not a list ``y``.** ``bar_stacked`` adds a third
column name, ``series``, so rows are ``(x, series, y)`` — exactly what
``GROUP BY month, kind`` already returns. ``y`` stays **one column, never a
list**, so the one-measure rule that makes a dual-axis chart unreachable needs no
exception: a stack adds a *dimension*, not a second *measure*.

**The two null-ish cases are handled oppositely, and that is the crux.** A cell
*absent* from a stacked result is filled with **0**: ``GROUP BY`` omits empty
groups, so an absent ``(x, series)`` pair means that category genuinely had none,
and filling it *recovers* a fact. A row *present* with ``y = None`` is
**refused**: a hole makes the bar's total wrong, and on a stack the total is the
whole point. On an unstacked kind a ``None`` ``y`` is neither — it leaves a gap,
because there the aggregate itself was null and zero would fabricate a value.
"""

from __future__ import annotations

CHART_KINDS = frozenset({"line", "bar", "bar_h", "bar_stacked", "bar_h_stacked"})
"""The closed set of drawable kinds. The registration list *is* the feature list."""

STACKED_KINDS = frozenset({"bar_stacked", "bar_h_stacked"})
"""The kinds that require ``series`` and forbid a ``None`` ``y``. See rules 8-10."""

MIN_ROWS_BY_KIND = {
    "bar": 2,
    "bar_h": 2,
    "line": 3,
    "bar_stacked": 2,
    "bar_h_stacked": 2,
}
"""Minimum plottable rows, per kind.

A two-bar comparison is legitimate — "these two developers" is a real question —
but a two-point line is a degenerate trend that should be a sentence. The
cataloged anti-pattern is the ONE-bar chart, not the two-bar one.

For stacked kinds this counts **distinct ``x`` values**, not rows: long format
means one ``x`` spans several rows, so ``len(rows)`` would let a one-bar chart
through whenever it happened to have two series.
"""

MIN_CHART_ROWS = min(MIN_ROWS_BY_KIND.values())
"""The ref-minting floor (2).

A producer withholds ``chart_ref`` below this, so the model never sees an
affordance it would be wrong to use. Kind-specific minimums are enforced here.
"""

MAX_SERIES = 6
"""Hard cap on distinct ``series`` values.

Six is the largest set that stays readable stacked *and* is validated as a
palette against the chat surface. Exceeding it **refuses**; the server never
folds an "other" bucket, because that would put a number on screen that no
emitted query produced — the one property this whole design exists to protect.
"""

MAX_X_TICKS = 12
"""The renderer's x-axis label budget.

Recorded here rather than only in the component so the chart contract lives in
one file: 200 category labels collide, and every value stays readable in the
table view regardless. Consumed by the frontend, not by :func:`validate_chart`.
"""

_MAX_TITLE = 80
_MAX_Y_LABEL = 40


class ChartNotAllowed(ValueError):
    """A directive that cannot be honoured, with a model-readable reason.

    The message is the contract: it names the offending value and the columns
    that *were* available, so the model can correct the call rather than losing
    the turn.
    """


def _available(columns: list[str]) -> str:
    """Render the producer's column names for a refusal message."""
    return ", ".join(repr(column) for column in columns) or "(none)"


def _resolve(name: object, role: str, columns: list[str]) -> int:
    """Resolve one column name to its index, or refuse naming what was available."""
    if not isinstance(name, str) or not name:
        raise ChartNotAllowed(
            f"{role} must be a column name (a string); got {name!r}. "
            f"Available columns: {_available(columns)}."
        )
    if name not in columns:
        raise ChartNotAllowed(
            f"unknown {role} column {name!r} — this result has columns "
            f"{_available(columns)}. Name a column of the result you charted, "
            "not a value from it."
        )
    return columns.index(name)


def validate_chart(
    *,
    kind: object,
    title: object,
    x: object,
    y: object,
    series: object = None,
    y_label: object = None,
    columns: list[str],
    rows: list[list],
) -> dict:
    """Normalize a chart directive, resolving column names to indices.

    Parameters
    ----------
    kind : str
        One of :data:`CHART_KINDS`.
    title : str
        Non-empty, at most 80 characters. Required because an unstacked chart has
        no legend, making the title its only identity channel.
    x : str
        Column name for the category / time axis.
    y : str
        Column name for the measure. **One column, never a list.**
    series : str, optional
        Required for :data:`STACKED_KINDS` and refused for every other kind: the
        column whose values become the stack segments.
    y_label : str, optional
        Axis caption, at most 40 characters.
    columns : list of str
        The producer's column names.
    rows : list of list
        The producer's rows, positional against ``columns``.

    Returns
    -------
    dict
        ``{"kind", "title", "x", "y", "x_index", "y_index", "null_rows"}``, plus
        ``"y_label"`` when given, plus for stacked kinds ``{"series",
        "series_index", "series_values", "x_values", "cells", "filled_cells"}``.

        ``series_values`` is the stack and colour-slot order — descending by
        series total, ties lexicographic — so a segment keeps its colour across
        re-renders and across the live-to-persisted swap. ``cells`` is the
        **densified** ``x`` × ``series`` grid with absent combinations filled
        with 0, and ``filled_cells`` counts them for the caption, so the fill is
        never silent.

    Raises
    ------
    ChartNotAllowed
        For any directive that cannot be honoured. The rows are unaffected and
        still on screen, so this degrades to correct numbers rather than a failed
        turn.
    """
    # Rule 1 — a closed set of kinds.
    if kind not in CHART_KINDS:
        raise ChartNotAllowed(
            f"unknown chart kind {kind!r} — use one of "
            f"{', '.join(sorted(CHART_KINDS))}."
        )
    stacked = kind in STACKED_KINDS

    # Rule 2 — the title is the only identity channel on an unstacked chart.
    if not isinstance(title, str) or not title.strip():
        raise ChartNotAllowed("title is required and must be a non-empty string.")
    title = title.strip()
    if len(title) > _MAX_TITLE:
        raise ChartNotAllowed(
            f"title is {len(title)} characters; keep it to {_MAX_TITLE} or fewer."
        )

    # Rule 7 — the optional axis caption.
    if y_label is not None:
        if not isinstance(y_label, str):
            raise ChartNotAllowed(f"y_label must be a string; got {y_label!r}.")
        y_label = y_label.strip()
        if len(y_label) > _MAX_Y_LABEL:
            raise ChartNotAllowed(
                f"y_label is {len(y_label)} characters; keep it to "
                f"{_MAX_Y_LABEL} or fewer."
            )

    # Rule 4a — one measure, structurally. Checked before any value is read, so
    # a list `y` cannot reach the numeric scan below.
    if isinstance(y, (list, tuple)):
        raise ChartNotAllowed(
            "y must be ONE column, not a list. Two measures means two charts — "
            "call render_chart twice. If you meant to break one measure down by "
            "a category, that is `series` on a stacked kind, not a second y."
        )

    # Rule 3 — both axes are grounded in the producer's own output.
    x_index = _resolve(x, "x", columns)
    y_index = _resolve(y, "y", columns)

    # Rule 4b — a chart of a column against itself is not a chart.
    if x == y:
        raise ChartNotAllowed(
            f"x and y are both {x!r}. Plotting a column against itself is not a "
            f"chart; pick two different columns from {_available(columns)}."
        )

    # Rule 8 — `series` is required by exactly the stacked kinds.
    series_index: int | None = None
    if stacked:
        if series is None:
            unstacked = "bar" if kind == "bar_stacked" else "bar_h"
            raise ChartNotAllowed(
                f"{kind} requires `series` — the column whose values become the "
                "stack segments, which means your SQL must GROUP BY both that "
                f"column and {x!r}. With no breakdown dimension this is just a "
                f"{unstacked}; use that kind instead. Available columns: "
                f"{_available(columns)}."
            )
        series_index = _resolve(series, "series", columns)
        if series in (x, y):
            raise ChartNotAllowed(
                f"series {series!r} is already used as "
                f"{'x' if series == x else 'y'}. The stack segments must be a "
                "third, different column."
            )
    elif series is not None:
        raise ChartNotAllowed(
            f"`series` is only valid on a stacked kind "
            f"({', '.join(sorted(STACKED_KINDS))}), not on {kind!r}. Drawing "
            f"{kind!r} while ignoring your breakdown would answer a different "
            "question than you asked — pick the stacked kind, or drop `series`."
        )

    # Rule 5 — the measure must be numeric. `bool` is excluded explicitly
    # because `isinstance(True, int)` is True in Python.
    null_rows = 0
    for row in rows:
        value = row[y_index]
        if value is None:
            null_rows += 1
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ChartNotAllowed(
                f"column {y!r} is not numeric — found {value!r}. A chart needs a "
                "measure; pick the counted or summed column, or swap x and y if "
                "the labels are on the wrong axis."
            )

    # Rule 10 — a present-but-NULL y on a stacked kind is refused, not zeroed.
    # This is the opposite of the absent-cell case below, deliberately: a hole
    # inside a bar makes its total a lie, and the total is what a stack is for.
    if stacked and null_rows:
        raise ChartNotAllowed(
            f"{null_rows} row(s) have no value for {y!r}, and a stacked chart "
            "cannot show a hole — every segment feeds a total. Wrap the measure "
            "in COALESCE(..., 0) if zero is the honest value, or filter those "
            "rows out. (An unstacked chart would draw them as gaps instead.)"
        )

    result: dict = {
        "kind": kind,
        "title": title,
        "x": x,
        "y": y,
        "x_index": x_index,
        "y_index": y_index,
        "null_rows": null_rows,
    }
    if y_label:
        result["y_label"] = y_label

    if stacked:
        # `series_index` is set: rule 8 refused above if `series` was missing.
        stack = _build_stack(
            rows=rows,
            x_index=x_index,
            y_index=y_index,
            series_index=series_index,
            series_name=series,
        )
        # Rule 9 — cardinality. Refuse; never fold a bucket the SQL did not emit.
        if len(stack["series_values"]) > MAX_SERIES:
            raise ChartNotAllowed(
                f"{len(stack['series_values'])} distinct {series!r} values "
                f"exceeds the {MAX_SERIES}-series limit — more segments than "
                "that is an unreadable smear. Fold the tail into an 'other' "
                "bucket in your SQL with a CASE, or filter to the top "
                f"{MAX_SERIES}. Do that in the query so every plotted number "
                "still comes from it."
            )
        result.update(stack)
        plottable = len(stack["x_values"])
    else:
        plottable = len(rows) - null_rows

    # Rule 6 — enough to be worth drawing. Counts distinct x on a stacked kind.
    minimum = MIN_ROWS_BY_KIND[kind]
    if plottable < minimum:
        unit = f"distinct {x!r} value(s)" if stacked else "plottable row(s)"
        advice = (
            "A two-point line is a degenerate trend — use `bar` to compare 2 values."
            if kind == "line"
            else "A single value is not a chart; just say the number."
        )
        raise ChartNotAllowed(
            f"{kind} needs at least {minimum} {unit}; this result has "
            f"{plottable}. {advice}"
        )

    return result


def _build_stack(
    *,
    rows: list[list],
    x_index: int,
    y_index: int,
    series_index: int,
    series_name: object,
) -> dict:
    """Densify a long-format result into an ``x`` × ``series`` grid.

    ``GROUP BY x, series`` omits empty groups, so a real result is sparse — on
    this repository the canonical stacked query returns 14 rows for a 4x5 grid.
    Leaving those cells out would shift later segments down and silently mislabel
    them, so the grid is filled here, **server-side**: one pivot implementation
    means the chart and the table view cannot disagree.

    Returns
    -------
    dict
        ``{"series", "series_index", "series_values", "x_values", "cells",
        "filled_cells"}``. ``x_values`` preserves the result's own row order, so
        the query's ``ORDER BY`` is the axis order.

    Raises
    ------
    ChartNotAllowed
        If one ``(x, series)`` pair appears twice — which means the query did not
        group by both columns. Summing them silently, or taking the last, would
        be a decision the emitted SQL never made.
    """
    x_values: list[str] = []
    series_totals: dict[str, float] = {}
    cells: dict[str, dict[str, float]] = {}

    for row in rows:
        x_value = str(row[x_index])
        series_value = str(row[series_index])
        value = row[y_index]
        if x_value not in cells:
            x_values.append(x_value)
            cells[x_value] = {}
        if series_value in cells[x_value]:
            raise ChartNotAllowed(
                f"({x_value!r}, {series_value!r}) appears more than once, so the "
                "result is not one row per (x, series) pair. GROUP BY both "
                f"columns — including {series_name!r} — in your SQL."
            )
        cells[x_value][series_value] = value
        series_totals[series_value] = series_totals.get(series_value, 0) + value

    # Descending by total, ties broken lexicographically. The tie-break is what
    # makes colour assignment reproducible: first-appearance order would let a
    # segment change colour between two renders of the same data.
    series_values = sorted(series_totals, key=lambda name: (-series_totals[name], name))

    filled_cells = 0
    for x_value in x_values:
        row_cells = cells[x_value]
        for series_value in series_values:
            if series_value not in row_cells:
                # Absent from a GROUP BY result *means* zero, so filling it
                # recovers a fact rather than inventing one. Counted, and
                # captioned by the renderer, so the fill is never invisible.
                row_cells[series_value] = 0
                filled_cells += 1

    return {
        "series": series_name,
        "series_index": series_index,
        "series_values": series_values,
        "x_values": x_values,
        "cells": cells,
        "filled_cells": filled_cells,
    }


__all__ = [
    "CHART_KINDS",
    "MAX_SERIES",
    "MAX_X_TICKS",
    "MIN_CHART_ROWS",
    "MIN_ROWS_BY_KIND",
    "STACKED_KINDS",
    "ChartNotAllowed",
    "validate_chart",
]
