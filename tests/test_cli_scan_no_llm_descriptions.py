"""Tests for ``whygraph scan --no-llm-descriptions``.

The crawlers themselves have dedicated tests (``test_git_crawler.py``,
``test_scan_analyze_crawler.py``). This module pins the flag wiring:
the option exists, the panel shows the right message, the
:class:`AnalyzeCrawler` is not constructed when the flag is set, and
the :class:`LlmDescriptor` probe is bypassed entirely so a broken
``[analyze]`` provider config does not block a flag-scan.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from whygraph import core
from whygraph.cli import main as whygraph_main
from whygraph.core.config import Config
from whygraph.db import ensure_initialized
from whygraph.db import engine as db_engine
from whygraph.services.llm import LlmError


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_repo(root: Path) -> Path:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("hello\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")
    return root


@pytest.fixture(autouse=True)
def _no_logging_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match ``test_cli_analyze.py``'s guard against process-wide logger mutation."""
    monkeypatch.setattr("whygraph.cli.configure_logging", lambda *a, **kw: None)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp git repo (no GitHub remote); cwd is moved into it."""
    root = _make_repo(tmp_path)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def isolated_db(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Per-test SQLite file with the WhyGraph schema migrated to head."""
    db_path = repo / ".whygraph" / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    ensure_initialized()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


class _RecordingCrawler:
    """Stand-in for a real crawler — records construction and no-ops on work."""

    last_kwargs: dict | None = None
    constructed: int = 0

    def __init__(self, _progress, **kwargs):
        type(self).last_kwargs = kwargs
        type(self).constructed += 1
        self.name = "recording"
        self.error = None

    def start(self) -> None:
        pass

    def join(self) -> None:
        pass


def _fresh_crawler_class() -> type[_RecordingCrawler]:
    """A new subclass per test so the class-level recorder is isolated."""
    return type("_RecordingCrawler", (_RecordingCrawler,), {})


@pytest.fixture
def stub_crawlers(monkeypatch: pytest.MonkeyPatch) -> dict[str, type]:
    """Patch every crawler class the CLI imports with recording subclasses."""
    git_cls = _fresh_crawler_class()
    github_cls = _fresh_crawler_class()
    analyze_cls = _fresh_crawler_class()
    # The CLI module imports GitCrawler + GitHubCrawler at module top.
    monkeypatch.setattr("whygraph.cli.commands.scan.GitCrawler", git_cls)
    monkeypatch.setattr("whygraph.cli.commands.scan.GitHubCrawler", github_cls)
    # AnalyzeCrawler is imported lazily inside scan_cmd — patch its source.
    monkeypatch.setattr("whygraph.scan.AnalyzeCrawler", analyze_cls)
    return {"git": git_cls, "github": github_cls, "analyze": analyze_cls}


@pytest.fixture
def no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the panel into the "origin is not a GitHub remote" branch."""
    monkeypatch.setattr(
        "whygraph.services.github.GitHubClient.for_repository",
        classmethod(lambda cls, _repo: None),
    )


def test_scan_no_llm_descriptions_skips_phase_two(
    isolated_db: Path,
    stub_crawlers: dict[str, type],
    no_github: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag set: no AnalyzeCrawler, no LlmDescriptor probe, panel updated."""
    probe_calls = []

    class _FailIfProbed:
        @classmethod
        def from_config(cls, _cfg):
            probe_calls.append(_cfg)
            return cls()

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _FailIfProbed)

    result = CliRunner().invoke(whygraph_main, ["scan", "--no-llm-descriptions"])

    assert result.exit_code == 0, result.output
    assert stub_crawlers["git"].constructed == 1
    assert stub_crawlers["analyze"].constructed == 0
    assert probe_calls == []  # descriptor probe bypassed entirely
    # Panel text — split because Rich may insert soft wraps.
    assert "skipped" in result.output
    assert "--no-llm-descriptions" in result.output


def test_scan_no_llm_descriptions_tolerates_broken_analyze_config(
    isolated_db: Path,
    stub_crawlers: dict[str, type],
    no_github: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken `[analyze]` provider must not block a flag-scan."""

    class _AlwaysFails:
        @classmethod
        def from_config(cls, _cfg):
            raise LlmError("provider 'broken' is not registered")

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _AlwaysFails)

    result = CliRunner().invoke(whygraph_main, ["scan", "--no-llm-descriptions"])

    assert result.exit_code == 0, result.output
    assert stub_crawlers["analyze"].constructed == 0


def test_scan_without_flag_still_constructs_analyze_crawler(
    isolated_db: Path,
    stub_crawlers: dict[str, type],
    no_github: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin: default behaviour still runs Phase 2."""

    class _FakeDescriptor:
        @classmethod
        def from_config(cls, _cfg):
            return cls()

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _FakeDescriptor)

    result = CliRunner().invoke(whygraph_main, ["scan"])

    assert result.exit_code == 0, result.output
    assert stub_crawlers["analyze"].constructed == 1
