"""Tests for :mod:`whygraph.portal.stats` and the runner's coverage snapshot.

``project_counts`` is the project details' ``stats`` and the body of a run's
``summary.coverage`` (M2f-3 plan sections 0.3 #11, 4.10).
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `scanner`) imported from sibling tests

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from test_portal_app import env, manual_ctx  # noqa: F401 -- `env` is a fixture
from test_portal_linked_scans import linked_project, whygraph_db
from test_portal_runner import (  # noqa: F401 -- `scanner` is a fixture
    client_for,
    first_scan,
    scanner,
)
from whygraph.core.context import use_project
from whygraph.db import ensure_initialized
from whygraph.db import get_session as project_session
from whygraph.db.models import RationaleCache
from whygraph.portal.migrate import ProjectMigrations
from whygraph.portal.stats import project_counts


def _card(path: str, start: int, end: int, provider: str, model: str) -> RationaleCache:
    return RationaleCache(
        path=path,
        line_start=start,
        line_end=end,
        provider=provider,
        model=model,
        evidence_fingerprint="fp",
        cached_at="2026-10-01T00:00:00+00:00",
        purpose="p",
        why="w",
        constraints="[]",
        tradeoffs="[]",
        risks="[]",
    )


def _state() -> SimpleNamespace:
    return SimpleNamespace(migrations=ProjectMigrations())


def test_rationale_cards_count_distinct_ranges(tmp_path: Path) -> None:
    """Two providers (or models) on one range are one card (BUG-26)."""
    ctx = manual_ctx(tmp_path)
    with use_project(ctx):
        ensure_initialized()
        with project_session() as session:
            session.add_all(
                [
                    _card("src/a.py", 1, 10, "anthropic", "m1"),
                    _card("src/a.py", 1, 10, "openai", "m2"),
                    _card("src/a.py", 1, 10, "anthropic", "m3"),
                    _card("src/a.py", 11, 20, "anthropic", "m1"),
                    _card("src/b.py", 1, 10, "anthropic", "m1"),
                ]
            )
            session.commit()

    assert project_counts(_state(), ctx) == {
        "commits": 0,
        "described": 0,
        "described_pct": 0.0,
        "pull_requests": 0,
        "issues": 0,
        "rationale_cards": 3,
    }


def test_no_counts_without_a_db_or_for_a_linked_project(tmp_path: Path) -> None:
    ctx = manual_ctx(tmp_path)
    assert project_counts(_state(), ctx) is None  # not created yet
    assert not (tmp_path / ".whygraph").exists()

    linked = replace(ctx, remote=object())  # type: ignore[arg-type]
    with use_project(ctx):
        ensure_initialized()
    assert project_counts(_state(), linked) is None  # never read locally


def test_a_linked_run_has_no_coverage_snapshot(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        root = linked_project(client, env, "lnk")
        run = first_scan(client, "lnk")
    assert "coverage" not in run["summary"]
    assert not whygraph_db(root).exists()
