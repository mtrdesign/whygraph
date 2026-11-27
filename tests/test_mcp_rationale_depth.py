"""The rationale depth resolver and its caller signal (plan 62 section 14.3).

The five-step table of :func:`whygraph.mcp.rationale_depth.resolve_depth` as
one row per case, then :func:`distinct_callers` against a small CodeGraph
fixture.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import build_fake_codegraph_db
from whygraph.core.config import Config, RationaleConfig
from whygraph.core.context import ProjectContext, use_project
from whygraph.mcp.rationale_depth import (
    IMPORTANT_CALLERS,
    DepthResolution,
    deeper,
    depth_block,
    distinct_callers,
    effective_ceiling,
    resolve_depth,
    resolve_for_target,
)
from whygraph.mcp.targets import Target

_BASE: dict[str, Any] = {
    "depth": None,
    "project_depth": "auto",
    "intent": None,
    "ceiling": "medium",
    "callers": 0,
    "pressure": None,
}

UNKNOWN = "The caller count is unknown, so importance did not count."
CEILING_LOW = "Capped at Low by the depth ceiling."

# (id, overrides, resolved, requested, reason)
ROWS: list[tuple[str, dict[str, Any], str, str, str]] = [
    # Step 1: fixed (explicit depth or a project depth), only step 5 applies.
    ("explicit", {"depth": "medium"}, "medium", "medium", "Medium was asked for."),
    (
        "explicit-above-ceiling-capped",
        {"depth": "medium", "ceiling": "low"},
        "low",
        "medium",
        f"Medium was asked for. {CEILING_LOW}",
    ),
    (
        "explicit-ignores-pressure",
        {"depth": "medium", "pressure": "project"},
        "medium",
        "medium",
        "Medium was asked for.",
    ),
    (
        "explicit-ignores-importance-and-intent",
        {"depth": "low", "intent": "refactor", "callers": 40},
        "low",
        "low",
        "Low was asked for.",
    ),
    (
        "explicit-beats-project-depth",
        {"depth": "low", "project_depth": "medium"},
        "low",
        "low",
        "Low was asked for.",
    ),
    (
        "project-depth",
        {"project_depth": "medium", "intent": "explain", "pressure": "org"},
        "medium",
        "medium",
        "Medium is the project depth.",
    ),
    (
        "project-depth-capped",
        {"project_depth": "medium", "ceiling": "low"},
        "low",
        "medium",
        f"Medium is the project depth. {CEILING_LOW}",
    ),
    # Step 2: the task base.
    (
        "explain",
        {"intent": "explain"},
        "low",
        "auto",
        'Auto chose Low because the task was "explain".',
    ),
    (
        "edit",
        {"intent": "edit"},
        "medium",
        "auto",
        'Auto chose Medium because the task was "edit".',
    ),
    (
        "plan",
        {"intent": "plan"},
        "medium",
        "auto",
        'Auto chose Medium because the task was "plan".',
    ),
    (
        "no-intent",
        {},
        "medium",
        "auto",
        "Auto chose Medium because no task was given.",
    ),
    # Refactor and delete reach the top offered level from the task alone:
    # the callers and the clamp are not mentioned (R49).
    (
        "refactor-with-callers",
        {"intent": "refactor", "callers": 2},
        "medium",
        "auto",
        'Auto chose Medium because the task was "refactor".',
    ),
    (
        "delete-without-callers",
        {"intent": "delete", "callers": 0},
        "medium",
        "auto",
        'Auto chose Medium because the task was "delete".',
    ),
    (
        "refactor-unknown-callers",
        {"intent": "refactor", "callers": None},
        "medium",
        "auto",
        'Auto chose Medium because the task was "refactor".',
    ),
    (
        "delete-many-callers",
        {"intent": "delete", "callers": 40},
        "medium",
        "auto",
        'Auto chose Medium because the task was "delete".',
    ),
    (
        "refactor-capped-by-ceiling",
        {"intent": "refactor", "callers": 3, "ceiling": "low"},
        "low",
        "auto",
        f'Auto chose Low because the task was "refactor". {CEILING_LOW}',
    ),
    (
        "base-capped-by-ceiling",
        {"intent": "edit", "ceiling": "low"},
        "low",
        "auto",
        f'Auto chose Low because the task was "edit". {CEILING_LOW}',
    ),
    # Step 3: importance.
    (
        "important-raises",
        {"intent": "explain", "callers": IMPORTANT_CALLERS},
        "medium",
        "auto",
        'Auto chose Medium because the task was "explain" and the symbol has 5'
        " distinct callers.",
    ),
    (
        "below-important-no-change",
        {"intent": "explain", "callers": IMPORTANT_CALLERS - 1},
        "low",
        "auto",
        'Auto chose Low because the task was "explain".',
    ),
    (
        "important-edit-stays-at-the-top",
        {"intent": "edit", "callers": 7},
        "medium",
        "auto",
        'Auto chose Medium because the task was "edit".',
    ),
    (
        "important-capped-by-ceiling",
        {"intent": "explain", "callers": 7, "ceiling": "low"},
        "low",
        "auto",
        'Auto chose Low because the task was "explain" and the symbol has 7'
        f" distinct callers. {CEILING_LOW}",
    ),
    (
        "unknown-callers-below-the-top",
        {"intent": "explain", "callers": None},
        "low",
        "auto",
        f'Auto chose Low because the task was "explain". {UNKNOWN}',
    ),
    (
        "unknown-callers-at-the-top-not-mentioned",
        {"intent": "edit", "callers": None},
        "medium",
        "auto",
        'Auto chose Medium because the task was "edit".',
    ),
    # Step 4: budget pressure, named when it lowers the level.
    (
        "pressure-lowers",
        {"intent": "edit", "pressure": "org"},
        "low",
        "auto",
        'Auto chose Low because the task was "edit" and the org budget is at'
        " 75 % or more.",
    ),
    (
        "pressure-at-the-floor-not-mentioned",
        {"intent": "explain", "pressure": "member"},
        "low",
        "auto",
        'Auto chose Low because the task was "explain".',
    ),
    (
        "importance-and-pressure-cancel",
        {"intent": "explain", "callers": 9, "pressure": "project"},
        "low",
        "auto",
        'Auto chose Low because the task was "explain", the symbol has 9'
        " distinct callers and the project budget is at 75 % or more.",
    ),
    # Step 5: clamped after each step, not once at the end.
    (
        "clamp-after-each-step",
        {"intent": "refactor", "callers": 9, "pressure": "project"},
        "low",
        "auto",
        'Auto chose Low because the task was "refactor" and the project budget'
        " is at 75 % or more.",
    ),
    (
        "clamp-after-each-step-edit",
        {"intent": "edit", "callers": 9, "pressure": "project"},
        "low",
        "auto",
        'Auto chose Low because the task was "edit" and the project budget is at'
        " 75 % or more.",
    ),
    (
        "clamp-after-each-step-by-ceiling",
        {"intent": "refactor", "callers": 9, "pressure": "org", "ceiling": "low"},
        "low",
        "auto",
        f'Auto chose Low because the task was "refactor". {CEILING_LOW}',
    ),
    # Review: low, any cached depth, steps 3-4 skipped.
    (
        "review-skips-importance-and-pressure",
        {"intent": "review", "callers": 40, "pressure": "org"},
        "low",
        "auto",
        'Auto chose Low because the task was "review". A cached card of any'
        " depth is accepted.",
    ),
    (
        "review-unknown-callers-not-mentioned",
        {"intent": "review", "callers": None},
        "low",
        "auto",
        'Auto chose Low because the task was "review". A cached card of any'
        " depth is accepted.",
    ),
    # Thin history is reported, never acted on.
    (
        "thin-history",
        {"intent": "edit", "thin_history": True},
        "medium",
        "auto",
        'Auto chose Medium because the task was "edit". History is thin (one'
        " commit, no PR).",
    ),
    (
        "thin-history-fixed",
        {"depth": "low", "thin_history": True},
        "low",
        "low",
        "Low was asked for. History is thin (one commit, no PR).",
    ),
]


@pytest.mark.parametrize(
    ("overrides", "resolved", "requested", "reason"),
    [row[1:] for row in ROWS],
    ids=[row[0] for row in ROWS],
)
def test_resolve_depth_table(
    overrides: dict[str, Any], resolved: str, requested: str, reason: str
) -> None:
    result = resolve_depth(**{**_BASE, **overrides})
    assert (result.resolved, result.requested, result.reason) == (
        resolved,
        requested,
        reason,
    )
    assert result.as_dict() == {
        "requested": requested,
        "resolved": resolved,
        "reason": reason,
    }


# (id, overrides, any_cached_depth, cached_only)
REVIEW_ROWS: list[tuple[str, dict[str, Any], bool, bool]] = [
    ("review-implies-cached-only", {"intent": "review"}, True, True),
    (
        "review-generate-mode",
        {"intent": "review", "cached_only": False},
        True,
        False,
    ),
    (
        "review-explicit-depth-keeps-its-depth",
        {"intent": "review", "depth": "medium"},
        False,
        True,
    ),
    ("other-intent-defaults-to-generate", {"intent": "edit"}, False, False),
    (
        "other-intent-keeps-cached-only",
        {"intent": "edit", "cached_only": True},
        False,
        True,
    ),
    ("no-intent", {}, False, False),
]


@pytest.mark.parametrize(
    ("overrides", "any_cached", "cached_only"),
    [row[1:] for row in REVIEW_ROWS],
    ids=[row[0] for row in REVIEW_ROWS],
)
def test_review_semantics(
    overrides: dict[str, Any], any_cached: bool, cached_only: bool
) -> None:
    result = resolve_depth(**{**_BASE, **overrides})
    assert (result.any_cached_depth, result.cached_only) == (any_cached, cached_only)


def test_review_with_explicit_depth_is_fixed() -> None:
    result = resolve_depth(**{**_BASE, "intent": "review", "depth": "medium"})
    assert result == DepthResolution(
        requested="medium",
        resolved="medium",
        reason="Medium was asked for.",
        any_cached_depth=False,
        cached_only=True,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"depth": "huge"},
        {"project_depth": "deep"},
        {"ceiling": "max"},
        {"intent": "debug"},
    ],
)
def test_unknown_values_raise(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        resolve_depth(**{**_BASE, **overrides})


@pytest.mark.parametrize(
    ("max_depth", "org_max_depth", "expected"),
    [
        ("medium", "medium", "medium"),
        ("medium", "low", "low"),
        ("low", "medium", "low"),
        ("low", "low", "low"),
    ],
)
def test_effective_ceiling_is_the_lower(
    max_depth: str, org_max_depth: str, expected: str
) -> None:
    rationale = RationaleConfig(max_depth=max_depth, org_max_depth=org_max_depth)
    assert effective_ceiling(rationale) == expected


# ---------------------------------------------------------------------------
# The caller signal
# ---------------------------------------------------------------------------


def _node(node_id: str, kind: str, qn: str, path: str, start: int, end: int) -> dict:
    return {
        "id": node_id,
        "kind": kind,
        "name": qn.rsplit(".", 1)[-1],
        "qualified_name": qn,
        "file_path": path,
        "language": "python",
        "start_line": start,
        "end_line": end,
        "docstring": None,
        "signature": None,
    }


_NODES = [
    _node("n_file", "file", "src/pkg/m.py", "src/pkg/m.py", 1, 80),
    _node("n_cls", "class", "pkg.m.Box", "src/pkg/m.py", 1, 40),
    _node("n_put", "method", "pkg.m.Box.put", "src/pkg/m.py", 10, 30),
    _node("n_lonely", "function", "pkg.m.lonely", "src/pkg/m.py", 45, 50),
    _node("n_a", "function", "pkg.a.a", "src/pkg/a.py", 1, 9),
    _node("n_b", "function", "pkg.b.b", "src/pkg/b.py", 1, 9),
    # Same name, another file: never the target.
    _node("n_other", "function", "pkg.m.Box.put", "src/pkg/other.py", 10, 30),
]
_EDGES = [
    ("n_a", "n_put", "calls", 3),
    ("n_a", "n_put", "calls", 7),  # a second edge from the same caller
    ("n_b", "n_put", "calls", 4),
    ("n_b", "n_cls", "calls", 5),
    ("n_cls", "n_put", "contains"),
    ("n_a", "n_other", "calls", 8),
]


@pytest.fixture
def bound(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    db = build_fake_codegraph_db(tmp_path / "codegraph.db", nodes=_NODES, edges=_EDGES)
    ctx = ProjectContext(slug="demo", root=root, config=Config(codegraph_db=db))
    with use_project(ctx):
        yield


@pytest.mark.parametrize(
    ("path", "start", "end", "expected"),
    [
        ("src/pkg/m.py", 12, 14, 2),  # Box.put: a twice and b, distinct ids
        ("src/pkg/m.py", 10, 30, 2),  # exactly the method
        ("src/pkg/m.py", 5, 35, 1),  # spans past put: the class, called by b
        ("src/pkg/m.py", 46, 47, 0),  # an indexed symbol nobody calls
        ("src/pkg/m.py", 60, 70, None),  # only the file node covers it
        ("src/pkg/missing.py", 1, 2, None),  # not indexed
    ],
)
def test_distinct_callers(
    bound: None, path: str, start: int, end: int, expected: int | None
) -> None:
    assert distinct_callers(path, start, end) == expected


def test_distinct_callers_without_an_index_is_unknown(tmp_path: Path) -> None:
    ctx = ProjectContext(
        slug="demo",
        root=tmp_path,
        config=Config(codegraph_db=tmp_path / "absent" / "codegraph.db"),
    )
    with use_project(ctx):
        assert distinct_callers("src/pkg/m.py", 12, 14) is None


# ---------------------------------------------------------------------------
# The card's depth and deeper (plan 62 section 14.4)
# ---------------------------------------------------------------------------


def _item(source: str, *, prs: int = 0) -> SimpleNamespace:
    """A stand-in for one ``CommitEvidence``: its ``source`` and PR count."""
    return SimpleNamespace(source=source, pull_requests=[object()] * prs)


def _target() -> Target:
    return Target(path="src/pkg/m.py", line_start=12, line_end=14, qualified_name=None)


def test_resolve_for_target_reads_the_bound_config_and_callers(bound: None) -> None:
    """Under auto, the index's distinct callers and the project's ceiling count."""
    resolution = resolve_for_target(_target(), [_item("blame")], intent="refactor")
    # Box.put has callers, but a refactor is Medium, the top offered level (R49).
    assert resolution.resolved == "medium"
    assert resolution.reason == (
        'Auto chose Medium because the task was "refactor". History is thin (one'
        " commit, no PR)."
    )
    review = resolve_for_target(_target(), [_item("blame", prs=1)], intent="review")
    assert (review.resolved, review.cached_only, review.any_cached_depth) == (
        "low",
        True,
        True,
    )


def test_resolve_for_target_without_own_lines_counts_no_callers(
    bound: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.mcp import rationale_depth

    def _never(*args: object) -> int:  # pragma: no cover - must not run
        raise AssertionError("callers counted on a requester's range")

    monkeypatch.setattr(rationale_depth, "distinct_callers", _never)
    resolution = resolve_for_target(
        _target(), [_item("blame", prs=1)], intent="explain", count_callers=False
    )
    assert resolution.resolved == "low"
    assert UNKNOWN in resolution.reason
    # Fixed depths read no signal at all.
    fixed = resolve_for_target(_target(), [_item("blame")], depth="low")
    assert fixed.resolved == "low"


def test_resolve_for_target_caps_at_the_effective_ceiling(tmp_path: Path) -> None:
    config = Config(
        rationale=RationaleConfig(max_depth="medium", org_max_depth="low"),
        codegraph_db=tmp_path / "absent.db",
    )
    with use_project(ProjectContext(slug="demo", root=tmp_path, config=config)):
        resolution = resolve_for_target(_target(), [_item("blame")], depth="medium")
    assert resolution.resolved == "low"
    assert "Capped at Low by the depth ceiling." in resolution.reason


def test_depth_block_says_a_deeper_card_was_kept() -> None:
    resolution = DepthResolution("auto", "low", "Auto chose Low.")

    kept = depth_block(resolution, "medium", "kept")
    cached = depth_block(resolution, "medium", "cached")

    assert kept["resolved"] == cached["resolved"] == "medium"
    assert kept["reason"] == (
        "Auto chose Low. Built at Medium to keep the deeper card of these lines."
    )
    assert cached["reason"] == "Auto chose Low. A cached Medium card was served."


@pytest.mark.parametrize(
    ("level", "served", "resolved", "sentence"),
    [
        ("medium", "cached", "medium", None),
        ("low", "cached", "low", "A cached Low card was served."),
    ],
)
def test_depth_block_reports_the_card_served(
    level: str, served: str, resolved: str, sentence: str | None
) -> None:
    resolution = DepthResolution("auto", "medium", "Auto chose Medium.")
    block = depth_block(resolution, level, served)
    assert set(block) == {"requested", "resolved", "reason"}
    assert block["resolved"] == resolved
    if sentence is None:
        assert block["reason"] == "Auto chose Medium."
    else:
        assert block["reason"].startswith("Auto chose Medium. " + sentence)


def test_deeper_names_what_the_next_level_adds() -> None:
    evidence = [
        _item("blame", prs=1),
        _item("blame-walked", prs=1),
        _item("pr-origin"),
        *[_item("area") for _ in range(9)],
    ]
    low = deeper("low", "medium", evidence)
    assert low == {
        "level": "medium",
        "adds": "Reads the PRs of the earlier commits that shaped these lines "
        "and reads 4 of the 9 other commits to this file.",
    }


def test_deeper_is_none_at_the_ceiling_or_with_nothing_to_add() -> None:
    lines_only = [_item("blame", prs=1)]
    rich = [_item("blame", prs=1), _item("pr-origin"), *[_item("area")] * 9]
    # Medium is the deepest level (R49, R51): nothing deeper, whatever the evidence.
    assert deeper("medium", "medium", rich) is None
    assert deeper("medium", "medium", lines_only) is None
    assert deeper("low", "low", rich) is None  # the ceiling
    assert deeper("low", "medium", lines_only) is None  # medium adds nothing here
