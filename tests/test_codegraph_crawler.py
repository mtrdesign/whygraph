"""Tests for :class:`whygraph.scan.codegraph_crawler.CodeGraphCrawler`.

The crawler wraps :func:`refresh_codegraph_index` in a progress-reporting
thread. These tests monkeypatch the refresh so no real ``codegraph`` /
Docker is needed, and drive the crawler via :meth:`Crawler.run` (the
thread entry point, run synchronously here) to exercise the base class's
error capture alongside the crawler's own best-effort handling.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.progress import Progress

from whygraph.scan import codegraph_crawler
from whygraph.scan.codegraph_crawler import CodeGraphCrawler
from whygraph.services.codegraph.exceptions import CodeGraphBootstrapError


def test_work_completes_bar_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, object] = {}

    def fake_refresh(
        project_root: Path, *, image: str | None = None, capture: bool = False
    ) -> Path:
        calls["project_root"] = project_root
        calls["capture"] = capture
        return project_root / ".codegraph" / "codegraph.db"

    monkeypatch.setattr(codegraph_crawler, "refresh_codegraph_index", fake_refresh)

    with Progress() as progress:
        crawler = CodeGraphCrawler(progress, project_root=tmp_path, image=None)
        crawler.run()
        task = progress.tasks[0]

    assert calls["project_root"] == tmp_path
    # The crawler always captures so output can't corrupt the live display.
    assert calls["capture"] is True
    assert crawler.error is None
    assert crawler.warning is None
    assert task.total == 1
    assert task.completed == 1
    assert task.finished


def test_bootstrap_error_swallowed_into_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_refresh(
        project_root: Path, *, image: str | None = None, capture: bool = False
    ) -> Path:
        raise CodeGraphBootstrapError("neither `codegraph` nor `docker` is on PATH")

    monkeypatch.setattr(codegraph_crawler, "refresh_codegraph_index", fake_refresh)

    with Progress() as progress:
        crawler = CodeGraphCrawler(progress, project_root=tmp_path, image=None)
        crawler.run()

    # Best-effort: a CodeGraph failure is a warning, never a scan failure.
    assert crawler.error is None
    assert crawler.warning is not None
    assert "CodeGraph refresh skipped" in crawler.warning
    assert "neither `codegraph`" in crawler.warning


def test_unexpected_error_propagates_to_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_refresh(
        project_root: Path, *, image: str | None = None, capture: bool = False
    ) -> Path:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(codegraph_crawler, "refresh_codegraph_index", fake_refresh)

    with Progress() as progress:
        crawler = CodeGraphCrawler(progress, project_root=tmp_path, image=None)
        crawler.run()

    # Only known bootstrap failures are best-effort; anything else is a
    # real crawler failure that should fail the scan.
    assert crawler.warning is None
    assert isinstance(crawler.error, RuntimeError)
