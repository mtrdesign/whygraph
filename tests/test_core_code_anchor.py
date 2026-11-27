"""Tests for ``whygraph.core.code_anchor`` (content anchors, R21 / R22)."""

from whygraph.core.code_anchor import (
    Anchor,
    anchor_from_diff_hunk,
    anchor_positions,
    anchor_status,
    normalize_code_line,
)

HUNK = (
    "@@ -1,4 +1,5 @@ def f():\n"
    " def handle(request):\n"
    "-    old_value = compute(request)\n"
    "+    value = compute_value(request)\n"
    "+    if value is None:\n"
    "+        raise ValueError(value)"
)


def test_header_skipped_and_minus_dropped():
    a = anchor_from_diff_hunk(HUNK)
    assert a is not None
    assert a.lines == (
        "value = compute_value(request)",
        "if value is None:",
        "raise ValueError(value)",
    )
    assert not any("old_value" in line or "@@" in line for line in a.lines)


def test_context_and_added_kept_no_newline_dropped():
    hunk = "@@ -1,2 +1,2 @@\n context_line_number_one()\n+added_line_number_two()\n\\ No newline at end of file"
    a = anchor_from_diff_hunk(hunk)
    assert a == Anchor(("context_line_number_one()", "added_line_number_two()"), True)


def test_last_three_non_blank_lines():
    hunk = "@@ -1 +1 @@\n+aaaaaaaaaa\n+\n+bbbbbbbbbb\n+cccccccccc\n+dddddddddd"
    a = anchor_from_diff_hunk(hunk)
    assert a is not None
    assert a.lines == ("bbbbbbbbbb", "cccccccccc", "dddddddddd")


def test_min_chars_boundary():
    assert anchor_from_diff_hunk("@@ -1 +1 @@\n+" + "x" * 19) is None
    assert anchor_from_diff_hunk("@@ -1 +1 @@\n+" + "x" * 20) is not None
    # whitespace does not count
    assert anchor_from_diff_hunk("@@ -1 +1 @@\n+" + "x " * 18 + "x") is None
    assert anchor_from_diff_hunk("@@ -1 +1 @@\n+" + "x " * 19 + "x") is not None


def test_empty_hunk():
    assert anchor_from_diff_hunk("") is None
    assert anchor_from_diff_hunk("@@ -1 +1 @@") is None


def test_added_flag_follows_last_line():
    assert (
        anchor_from_diff_hunk(
            "@@ -1 +1 @@\n+first_line_here()\n last_context_line()"
        ).added
        is False
    )
    assert (
        anchor_from_diff_hunk(
            "@@ -1 +1 @@\n first_context_line()\n+last_added_line()"
        ).added
        is True
    )


def test_normalisation():
    assert normalize_code_line("\t a \t  b   c\r") == "a b c"
    a = anchor_from_diff_hunk("@@ -1 +1 @@\n+\tif   some_condition(x):\r")
    assert a is not None and a.lines == ("if some_condition(x):",)


FILE = [
    "def f():",
    "    a = 1",
    "",
    "    b = 2\r",
    "    c = 3",
    "    a = 1",
    "    b = 2",
    "    c = 3",
]


def test_consecutive_run_match_skips_blanks_and_keeps_numbers():
    assert anchor_positions(["a = 1", "b = 2"], FILE) == [4, 7]


def test_split_run_not_matched():
    assert anchor_positions(["a = 1", "c = 3"], FILE) == []


def test_none_and_empty_inputs():
    assert anchor_positions(None, FILE) == []
    assert anchor_positions(["a = 1"], None) == []
    assert anchor_positions([], FILE) == []
    assert anchor_positions(["a = 1"], []) == []


def test_last_line_position_even_when_earlier_lines_above_range():
    lines = ["x", "def target(a):", "    return a + 1", "", "y"]
    assert anchor_positions(["def target(a):", "return a + 1"], lines) == [3]


def test_anchor_status():
    assert anchor_status(["a = 1", "b = 2"], ["a = 1", "b = 2", "z"]) is True
    assert anchor_status(["a = 1", "q = 9"], FILE) is False
    assert anchor_status(["a = 1", "b = 2"], FILE) is None
    assert anchor_status(None, FILE) is None
    assert anchor_status(["a = 1"], None) is None
