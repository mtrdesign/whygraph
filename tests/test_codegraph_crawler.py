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


def test_refresh_runs_under_the_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.services.codegraph import writer_lock

    held: list[bool] = []

    def fake_refresh(project_root: Path, **_kw: object) -> Path:
        with writer_lock(project_root, wait=0) as again:
            held.append(not again)  # a second taker is refused while we run
        return project_root / ".codegraph" / "codegraph.db"

    monkeypatch.setattr(codegraph_crawler, "refresh_codegraph_index", fake_refresh)
    monkeypatch.delenv("WHYGRAPH_CODEGRAPH_LOCK_DIR", raising=False)

    with Progress() as progress:
        crawler = CodeGraphCrawler(progress, project_root=tmp_path, image=None)
        crawler.run()

    assert held == [True]
    assert crawler.error is None and crawler.warning is None


def test_a_busy_writer_lock_is_a_warning_or_a_strict_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.services.codegraph import writer_lock

    monkeypatch.setattr(codegraph_crawler, "WRITER_LOCK_WAIT_SEC", 0.05)
    monkeypatch.setattr(
        codegraph_crawler,
        "refresh_codegraph_index",
        lambda *a, **k: pytest.fail("must not run without the lock"),
    )
    monkeypatch.delenv("WHYGRAPH_CODEGRAPH_LOCK_DIR", raising=False)

    with writer_lock(tmp_path, wait=0), Progress() as progress:
        lax = CodeGraphCrawler(progress, project_root=tmp_path, image=None)
        lax.run()
        strict = CodeGraphCrawler(
            progress, project_root=tmp_path, image=None, strict=True
        )
        strict.run()

    assert lax.error is None and lax.warning and "writer" in lax.warning
    assert strict.error is not None


@pytest.mark.parametrize("strict", [False, True])
def test_a_managed_child_without_the_lock_dir_never_runs_codegraph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    monkeypatch.delenv("WHYGRAPH_CODEGRAPH_LOCK_DIR", raising=False)
    monkeypatch.setattr(
        codegraph_crawler,
        "refresh_codegraph_index",
        lambda *a, **k: pytest.fail("CodeGraph must not run unlocked"),
    )

    with Progress() as progress:
        crawler = CodeGraphCrawler(
            progress,
            project_root=tmp_path,
            image=None,
            strict=strict,
            require_lock_dir=True,
        )
        crawler.run()

    message = crawler.error if strict else crawler.warning
    assert message is not None and "writer lock directory not configured" in str(
        message
    )
