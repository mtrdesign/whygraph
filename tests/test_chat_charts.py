"""The chart-directive validator — one case per rule.

The messages are asserted, not just the refusals. Column-name hallucination is
the dominant failure mode for a two-step chart design (the model must *recall*
the names rather than read them), and the only defense is a refusal that names
what was available, delivered as a tool result the model can act on in-loop. A
refusal with an unhelpful message is a failed turn wearing a passing test.
"""

from __future__ import annotations

import pytest

from whygraph.chat.charts import (
    CHART_KINDS,
    MAX_SERIES,
    MIN_CHART_ROWS,
    MIN_ROWS_BY_KIND,
    STACKED_KINDS,
    ChartNotAllowed,
    validate_chart,
)

_COLUMNS = ["month", "commits"]
_ROWS = [["2026-03", 41], ["2026-04", 38], ["2026-05", 52], ["2026-06", 47]]

# The real 14-row / 4x5 result: `GROUP BY month, change_type` over
# commit_file_change. Six of the twenty cells are missing, because April had no
# deletions and June had no renames or copies. Every stacked case below runs
# against this, so the tests exercise the sparsity rather than a tidy grid.
_STACK_COLUMNS = ["month", "kind", "n"]
_STACK_ROWS = [
    ["2026-03", "M", 40],
    ["2026-03", "A", 12],
    ["2026-03", "D", 5],
    ["2026-03", "R", 2],
    ["2026-03", "C", 1],
    ["2026-04", "M", 30],
    ["2026-04", "A", 9],
    ["2026-04", "R", 1],
    ["2026-04", "C", 1],
    ["2026-05", "M", 55],
    ["2026-05", "A", 20],
    ["2026-05", "D", 7],
    ["2026-06", "M", 18],
    ["2026-06", "A", 4],
]


def _stack(**overrides) -> dict:
    kwargs = {
        "kind": "bar_stacked",
        "title": "File changes per month by type",
        "x": "month",
        "y": "n",
        "series": "kind",
        "columns": _STACK_COLUMNS,
        "rows": _STACK_ROWS,
    }
    kwargs.update(overrides)
    return validate_chart(**kwargs)


# ---------------------------------------------------------------------------
# The happy path, and the contract of the returned dict
# ---------------------------------------------------------------------------


def test_a_valid_directive_resolves_columns_to_indices() -> None:
    chart = validate_chart(
        kind="line",
        title="Commits per month",
        x="month",
        y="commits",
        y_label="commits",
        columns=_COLUMNS,
        rows=_ROWS,
    )
    assert chart == {
        "kind": "line",
        "title": "Commits per month",
        "x": "month",
        "y": "commits",
        "x_index": 0,
        "y_index": 1,
        "y_label": "commits",
        "null_rows": 0,
    }


def test_an_unstacked_chart_carries_no_stack_keys() -> None:
    """The renderer branches on their presence, so they must be absent."""
    chart = validate_chart(
        kind="bar",
        title="Commits",
        x="month",
        y="commits",
        columns=_COLUMNS,
        rows=_ROWS,
    )
    for key in ("series", "series_values", "cells", "filled_cells", "x_values"):
        assert key not in chart


def test_the_ref_minting_floor_matches_the_cheapest_kind() -> None:
    """A producer withholds `chart_ref` below `MIN_CHART_ROWS`.

    If the floor were higher than a kind's own minimum, that kind would be
    unreachable: no ref would ever be minted for a result it could draw.
    """
    assert MIN_CHART_ROWS == min(MIN_ROWS_BY_KIND.values()) == 2
    assert set(MIN_ROWS_BY_KIND) == CHART_KINDS


# ---------------------------------------------------------------------------
# Rule 1 — the closed kind set
# ---------------------------------------------------------------------------


def test_an_unknown_kind_is_refused_and_lists_the_legal_ones() -> None:
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="scatter",
            title="t",
            x="month",
            y="commits",
            columns=_COLUMNS,
            rows=_ROWS,
        )
    message = str(excinfo.value)
    assert "scatter" in message
    for kind in CHART_KINDS:
        assert kind in message


def test_pie_is_still_refused() -> None:
    """Excluded on design grounds: part-to-whole rides the stacked bar.

    Asking for one must degrade to correct numbers plus a readable reason, not a
    failed turn — so this is a refusal with a message, never an exception.
    """
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="pie", title="t", x="month", y="commits", columns=_COLUMNS, rows=_ROWS
        )
    assert "bar_stacked" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Rule 2 / rule 7 — the text fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", [None, "", "   ", 7])
def test_a_missing_title_is_refused(title: object) -> None:
    """On an unstacked chart there is no legend, so the title is the only key."""
    with pytest.raises(ChartNotAllowed, match="title is required"):
        validate_chart(
            kind="bar",
            title=title,
            x="month",
            y="commits",
            columns=_COLUMNS,
            rows=_ROWS,
        )


def test_an_overlong_title_is_refused_with_its_length() -> None:
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="bar",
            title="x" * 81,
            x="month",
            y="commits",
            columns=_COLUMNS,
            rows=_ROWS,
        )
    assert "81 characters" in str(excinfo.value)


def test_an_overlong_y_label_is_refused() -> None:
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="bar",
            title="t",
            x="month",
            y="commits",
            y_label="y" * 41,
            columns=_COLUMNS,
            rows=_ROWS,
        )
    assert "41 characters" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Rule 3 — the columns must be the producer's own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("x", "y", "role"),
    [("week", "commits", "x"), ("month", "count", "y")],
)
def test_an_unknown_column_names_what_was_available(x: str, y: str, role: str) -> None:
    """The primary defense against field hallucination."""
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(kind="bar", title="t", x=x, y=y, columns=_COLUMNS, rows=_ROWS)
    message = str(excinfo.value)
    assert f"unknown {role} column" in message
    # Both available names appear verbatim, so the correction is mechanical.
    assert "'month'" in message and "'commits'" in message


def test_a_non_string_column_is_refused_rather_than_coerced() -> None:
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="bar", title="t", x=0, y="commits", columns=_COLUMNS, rows=_ROWS
        )
    assert "must be a column name" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Rule 4 — one measure, structurally
# ---------------------------------------------------------------------------


def test_a_list_y_is_refused_and_says_two_charts() -> None:
    """This is what makes a dual-axis chart unreachable rather than discouraged."""
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="line",
            title="t",
            x="month",
            y=["insertions", "deletions"],
            columns=["month", "insertions", "deletions"],
            rows=[["2026-03", 1, 2], ["2026-04", 3, 4], ["2026-05", 5, 6]],
        )
    message = str(excinfo.value)
    assert "two charts" in message
    # And it must point at the right alternative for a *breakdown*, so the model
    # does not "fix" a genuine stack by asking for two charts.
    assert "series" in message


def test_a_list_y_is_refused_before_its_values_are_read() -> None:
    """Rule ordering: rule 5 would otherwise index into a list of column names."""
    with pytest.raises(ChartNotAllowed, match="ONE column"):
        validate_chart(
            kind="bar",
            title="t",
            x="month",
            y=["commits"],
            columns=_COLUMNS,
            rows=_ROWS,
        )


def test_x_equal_to_y_is_refused() -> None:
    with pytest.raises(ChartNotAllowed, match="against itself"):
        validate_chart(
            kind="bar",
            title="t",
            x="commits",
            y="commits",
            columns=_COLUMNS,
            rows=_ROWS,
        )


# ---------------------------------------------------------------------------
# Rule 5 — the measure must be numeric
# ---------------------------------------------------------------------------


def test_a_text_y_column_is_refused() -> None:
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="bar",
            title="t",
            x="commits",
            y="month",
            columns=_COLUMNS,
            rows=_ROWS,
        )
    message = str(excinfo.value)
    assert "not numeric" in message
    assert "swap x and y" in message


def test_a_bool_y_column_is_refused_despite_being_an_int() -> None:
    """`isinstance(True, int)` is True in Python, so `bool` needs excluding."""
    with pytest.raises(ChartNotAllowed, match="not numeric"):
        validate_chart(
            kind="bar",
            title="t",
            x="flag",
            y="merged",
            columns=["flag", "merged"],
            rows=[["a", True], ["b", False]],
        )


def test_floats_are_accepted() -> None:
    chart = validate_chart(
        kind="line",
        title="PR cycle time",
        x="month",
        y="days",
        columns=["month", "days"],
        rows=[["2026-03", 0.4], ["2026-04", 1.25], ["2026-05", 2.0]],
    )
    assert chart["y_index"] == 1


# ---------------------------------------------------------------------------
# Rule 3 (NULL handling) — a gap on unstacked kinds, never a zero
# ---------------------------------------------------------------------------


def test_null_y_rows_are_counted_not_zeroed() -> None:
    """Zero-substitution would fabricate a data point that no query produced."""
    chart = validate_chart(
        kind="line",
        title="t",
        x="month",
        y="commits",
        columns=_COLUMNS,
        rows=[
            ["2026-03", 41],
            ["2026-04", None],
            ["2026-05", 52],
            ["2026-06", None],
            ["2026-07", 12],
        ],
    )
    assert chart["null_rows"] == 2


def test_an_all_null_measure_is_refused_as_too_few_rows() -> None:
    """Nothing is plottable, so there is nothing to draw — say so, don't draw 0s."""
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="line",
            title="t",
            x="month",
            y="commits",
            columns=_COLUMNS,
            rows=[["2026-03", None], ["2026-04", None], ["2026-05", None]],
        )
    assert "plottable row(s)" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Rule 6 — enough to be worth drawing
# ---------------------------------------------------------------------------


def test_two_rows_are_enough_for_a_bar_but_not_a_line() -> None:
    """The cataloged anti-pattern is the ONE-bar chart, not the two-bar one.

    "Which of these two developers has been busier" is a real question; a
    two-point line is a degenerate trend that should be a sentence.
    """
    two = [["alice", 12], ["bob", 9]]
    assert (
        validate_chart(
            kind="bar",
            title="t",
            x="month",
            y="commits",
            columns=_COLUMNS,
            rows=two,
        )["null_rows"]
        == 0
    )

    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="line", title="t", x="month", y="commits", columns=_COLUMNS, rows=two
        )
    assert "use `bar` to compare 2 values" in str(excinfo.value)


def test_a_single_row_is_refused_for_every_kind() -> None:
    for kind in sorted(CHART_KINDS):
        kwargs = {
            "kind": kind,
            "title": "t",
            "x": "month",
            "y": "commits",
            "columns": _COLUMNS,
            "rows": [["2026-03", 41]],
        }
        if kind in STACKED_KINDS:
            kwargs.update(
                series="kind",
                columns=_STACK_COLUMNS,
                rows=[["2026-03", "M", 40], ["2026-03", "A", 12]],
            )
        with pytest.raises(ChartNotAllowed):
            validate_chart(**kwargs)


# ---------------------------------------------------------------------------
# Rules 8-10 and densification — the stacked kinds
# ---------------------------------------------------------------------------


def test_a_sparse_stacked_result_is_densified_to_a_full_grid() -> None:
    """The case that proves densification necessary: 14 rows over a 4x5 grid.

    `GROUP BY` omits empty groups, so six cells are simply absent. Leaving them
    out would shift the segments above them down and mislabel every one.
    """
    chart = _stack()
    assert chart["x_values"] == ["2026-03", "2026-04", "2026-05", "2026-06"]
    assert chart["series_values"] == ["M", "A", "D", "R", "C"]
    assert chart["filled_cells"] == 6
    assert sum(len(cells) for cells in chart["cells"].values()) == 20
    # The specific holes, filled with 0 — not omitted, not None.
    assert chart["cells"]["2026-04"]["D"] == 0
    assert chart["cells"]["2026-06"]["R"] == 0
    assert chart["cells"]["2026-06"]["C"] == 0


def test_densification_changes_no_number() -> None:
    """Filling absent cells with 0 must leave every per-x total untouched.

    This is the assertion that separates "recovered a fact" from "invented one".
    """
    chart = _stack()
    expected: dict[str, int] = {}
    for month, _kind, n in _STACK_ROWS:
        expected[month] = expected.get(month, 0) + n
    for x_value, cells in chart["cells"].items():
        assert sum(cells.values()) == expected[x_value]


def test_a_present_null_y_is_refused_on_a_stacked_kind() -> None:
    """The opposite of the absent-cell case, and deliberately so.

    An absent cell means the group was empty; a NULL aggregate means the value is
    unknown. Drawing the second as zero makes the bar's total wrong, and the
    total is what a stack is for.
    """
    with pytest.raises(ChartNotAllowed) as excinfo:
        _stack(rows=[*_STACK_ROWS[:-1], ["2026-06", "A", None]])
    message = str(excinfo.value)
    assert "COALESCE" in message
    assert "cannot show a hole" in message
    # And it must say what the unstacked kinds would have done instead.
    assert "gaps" in message


def test_the_same_null_is_a_gap_on_an_unstacked_kind() -> None:
    """The two behaviours, side by side on the same data."""
    chart = validate_chart(
        kind="bar",
        title="t",
        x="month",
        y="n",
        columns=_STACK_COLUMNS,
        rows=[*_STACK_ROWS[:-1], ["2026-06", "A", None]],
    )
    assert chart["null_rows"] == 1


def test_series_is_required_on_a_stacked_kind() -> None:
    with pytest.raises(ChartNotAllowed) as excinfo:
        _stack(series=None)
    message = str(excinfo.value)
    # Names the unstacked kind to use instead, so the model has a way forward.
    assert "use that kind instead" in message
    assert "bar" in message
    assert "GROUP BY" in message


def test_the_horizontal_stacked_kind_names_bar_h_as_its_fallback() -> None:
    with pytest.raises(ChartNotAllowed, match="bar_h"):
        _stack(kind="bar_h_stacked", series=None)


def test_series_is_refused_on_an_unstacked_kind() -> None:
    """Ignoring it would draw a chart answering a different question."""
    with pytest.raises(ChartNotAllowed) as excinfo:
        validate_chart(
            kind="bar",
            title="t",
            x="month",
            y="n",
            series="kind",
            columns=_STACK_COLUMNS,
            rows=_STACK_ROWS,
        )
    message = str(excinfo.value)
    assert "only valid on a stacked kind" in message
    assert "different question" in message


@pytest.mark.parametrize("collision", ["month", "n"])
def test_series_cannot_reuse_the_x_or_y_column(collision: str) -> None:
    with pytest.raises(ChartNotAllowed, match="third, different column"):
        _stack(series=collision)


def test_an_unknown_series_column_names_what_was_available() -> None:
    with pytest.raises(ChartNotAllowed) as excinfo:
        _stack(series="change_type")
    message = str(excinfo.value)
    assert "unknown series column" in message
    assert "'kind'" in message


def test_exactly_six_series_is_accepted() -> None:
    """The boundary, from the palette's six validated slots."""
    rows = [[f"x{i}", f"s{j}", j + 1] for i in range(2) for j in range(MAX_SERIES)]
    chart = _stack(columns=["x", "s", "n"], x="x", y="n", series="s", rows=rows)
    assert len(chart["series_values"]) == MAX_SERIES
    assert chart["filled_cells"] == 0


def test_a_seventh_series_is_refused_and_says_to_fold_in_sql() -> None:
    """The server never folds an 'other' bucket itself.

    Doing so would put a number on screen that no emitted query produced, which
    is the one property this whole design exists to protect.
    """
    rows = [[f"x{i}", f"s{j}", j + 1] for i in range(2) for j in range(MAX_SERIES + 1)]
    with pytest.raises(ChartNotAllowed) as excinfo:
        _stack(columns=["x", "s", "n"], x="x", y="n", series="s", rows=rows)
    message = str(excinfo.value)
    assert f"{MAX_SERIES + 1} distinct" in message
    assert "'other'" in message
    assert "in your SQL" in message
    assert "CASE" in message


def test_series_order_is_descending_by_total_with_a_lexicographic_tie_break() -> None:
    """Colour slot i is `series_values[i]`, so the order must be deterministic.

    First-appearance order would let a segment change colour between two renders
    of the same data. The tie-break is what makes that reproducible.
    """
    rows = [
        ["x1", "zulu", 10],
        ["x1", "alpha", 10],  # ties zulu on total — must sort first
        ["x1", "mike", 30],
        ["x2", "zulu", 1],
        ["x2", "alpha", 1],
        ["x2", "mike", 1],
    ]
    chart = _stack(columns=["x", "s", "n"], x="x", y="n", series="s", rows=rows)
    assert chart["series_values"] == ["mike", "alpha", "zulu"]


def test_the_same_result_validated_twice_gives_the_same_colour_order() -> None:
    assert _stack()["series_values"] == _stack()["series_values"]


def test_a_stacked_kind_counts_distinct_x_not_rows() -> None:
    """`len(rows)` would let a one-bar chart through on multi-series data."""
    with pytest.raises(ChartNotAllowed) as excinfo:
        _stack(
            rows=[["2026-03", "M", 40], ["2026-03", "A", 12], ["2026-03", "D", 5]],
        )
    message = str(excinfo.value)
    assert "distinct 'month' value(s)" in message
    assert "this result has 1" in message


def test_a_repeated_x_series_pair_is_refused() -> None:
    """Two rows for one cell means the query did not group by both columns.

    Summing them, or taking the last, would be a decision the SQL never made.
    """
    with pytest.raises(ChartNotAllowed) as excinfo:
        _stack(rows=[*_STACK_ROWS, ["2026-03", "M", 99]])
    message = str(excinfo.value)
    assert "more than once" in message
    assert "GROUP BY both" in message


def test_the_x_axis_keeps_the_querys_own_order() -> None:
    """The result's ORDER BY is the axis order — not a re-sort here."""
    chart = _stack(
        rows=[
            ["2026-06", "M", 18],
            ["2026-03", "M", 40],
            ["2026-06", "A", 4],
            ["2026-03", "A", 12],
        ]
    )
    assert chart["x_values"] == ["2026-06", "2026-03"]
