"""Tests for :func:`whygraph.analyze.diff_split.split_into_chunks`.

Pure string logic — no git, no LLM. Each test feeds a diff string and a
character budget and asserts on the returned chunk list.
"""

from __future__ import annotations

from whygraph.analyze.diff_split import split_into_chunks


def _file(name: str, body: str) -> str:
    """Build one file's section of a unified diff, newline-terminated."""
    return f"diff --git a/{name} b/{name}\n{body}\n"


# ---- file-boundary splitting --------------------------------------------


def test_splits_on_each_file_header() -> None:
    diff = _file("a.py", "+a") + _file("b.py", "+b") + _file("c.py", "+c")
    # A budget of 1 forces every file into its own chunk.
    chunks = split_into_chunks(diff, max_chars=1)
    assert len(chunks) == 3
    assert all(chunk.startswith("diff --git ") for chunk in chunks)


def test_chunks_concatenate_back_to_the_original_diff() -> None:
    diff = _file("a.py", "+a") + _file("b.py", "+b") + _file("c.py", "+c")
    for budget in (1, 30, 1000):
        assert "".join(split_into_chunks(diff, max_chars=budget)) == diff


# ---- bin-packing ---------------------------------------------------------


def test_small_files_pack_into_one_chunk() -> None:
    diff = _file("a.py", "+a") + _file("b.py", "+b") + _file("c.py", "+c")
    chunks = split_into_chunks(diff, max_chars=10_000)
    assert chunks == [diff]


def test_chunk_closes_before_exceeding_budget() -> None:
    a, b, c = _file("a", "x"), _file("b", "y"), _file("c", "z")
    # a + b fit; adding c would overflow, so c starts a fresh chunk.
    budget = len(a) + len(b) + 1
    chunks = split_into_chunks(a + b + c, max_chars=budget)
    assert chunks == [a + b, c]
    assert all(len(chunk) <= budget for chunk in chunks)


# ---- oversized single file ----------------------------------------------


def test_file_larger_than_budget_is_its_own_chunk_unsplit() -> None:
    big = _file("big", "x" * 500)
    small = _file("small", "y")
    chunks = split_into_chunks(big + small, max_chars=50)
    assert chunks == [big, small]
    # The oversized file is handed back whole — the caller truncates it.
    assert len(chunks[0]) > 50


# ---- fallbacks -----------------------------------------------------------


def test_diff_without_file_headers_is_a_single_chunk() -> None:
    blob = "@@ -1 +1 @@\n-old\n+new\n"
    assert split_into_chunks(blob, max_chars=5) == [blob]


def test_preamble_before_first_header_folds_into_first_chunk() -> None:
    preamble = "warning: leading text\n"
    diff = preamble + _file("a.py", "+a") + _file("b.py", "+b")
    chunks = split_into_chunks(diff, max_chars=1)  # one file per chunk
    assert len(chunks) == 2
    assert chunks[0].startswith(preamble)
    assert "".join(chunks) == diff
