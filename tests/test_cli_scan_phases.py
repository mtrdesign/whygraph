"""Tests for ``whygraph scan``'s three-phase orchestration and output.

Pins the phase *sequencing* — Phase 1 (git + GitHub, concurrent) → Phase 2
(pr-origins) → Phase 3 (analyze, the LLM long pole, last and alone) — plus
the numbered phase headers and the closing results panel. The crawlers are
stubbed with recording stand-ins so no git / GitHub / LLM / CodeGraph work
actually runs; only the orchestrator's ordering and rendering is exercised.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner
from rich.console import Console

from whygraph import core
from whygraph.cli import main as whygraph_main
from whygraph.cli.commands import scan as scan_mod
from whygraph.core.config import Config
from whygraph.db import ensure_initialized
from whygraph.db import engine as db_engine


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
    monkeypatch.setattr("whygraph.cli.configure_logging", lambda *a, **kw: None)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = _make_repo(tmp_path)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def isolated_db(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db_path = repo / ".whygraph" / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    ensure_initialized()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


class _DummyClient:
    """A resolved GitHub client so Phase 1's GitHub + Phase 2 pr-origins run."""

    owner = "acme"
    name = "widgets"
    pull_requests: tuple = ()
    issues: tuple = ()


class _DummyDescriptor:
    """Non-None descriptor so the LLM phase runs (probe not exercised)."""

    @classmethod
    def from_config(cls, _cfg: object) -> "_DummyDescriptor":
        return cls()


def _stub(name: str, order: list[tuple[str, str]]) -> type:
    """A recording crawler class bound to ``name``, logging start/join order."""

    class _Stub:
        constructed = 0

        def __init__(self, _progress: object, **_kwargs: object) -> None:
            type(self).constructed += 1
            self.name = name
            self.error = None
            self.warning = None
            self.summary = f"{name} ok"

        def start(self) -> None:
            order.append(("start", name))

        def join(self, timeout: float | None = None) -> None:
            order.append(("join", name))

    return _Stub


def _patch_crawlers(
    monkeypatch: pytest.MonkeyPatch, order: list[tuple[str, str]]
) -> dict[str, type]:
    """Replace every crawler class the CLI touches with a recording stub."""
    stubs = {
        n: _stub(n, order)
        for n in ("git", "github", "pr-origins", "analyze", "codegraph")
    }
    monkeypatch.setattr(scan_mod, "GitCrawler", stubs["git"])
    monkeypatch.setattr(scan_mod, "GitHubCrawler", stubs["github"])
    monkeypatch.setattr(scan_mod, "PROriginEnricher", stubs["pr-origins"])
    monkeypatch.setattr(scan_mod, "CodeGraphCrawler", stubs["codegraph"])
    # AnalyzeCrawler is imported lazily inside scan_cmd — patch its source.
    monkeypatch.setattr("whygraph.scan.AnalyzeCrawler", stubs["analyze"])
    return stubs


def _idx(order: list[tuple[str, str]], event: tuple[str, str]) -> int:
    return order.index(event)


def test_three_phases_run_in_order_with_llm_last(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[tuple[str, str]] = []
    stubs = _patch_crawlers(monkeypatch, order)
    monkeypatch.setattr(
        scan_mod, "_select_github_client", lambda *a, **k: _DummyClient()
    )
    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _DummyDescriptor)

    result = CliRunner().invoke(whygraph_main, ["scan"])

    assert result.exit_code == 0, result.output
    # All five crawlers constructed exactly once.
    for name in ("git", "github", "pr-origins", "analyze", "codegraph"):
        assert stubs[name].constructed == 1, name

    # Numbered headers for all three phases.
    assert "Phase 1/3 · Structural crawl" in result.output
    assert "Phase 2/3 · PR-origin recovery" in result.output
    assert "Phase 3/3 · LLM descriptions" in result.output

    # CodeGraph is a background task: started first, joined last.
    assert order[0] == ("start", "codegraph")
    assert order[-1] == ("join", "codegraph")

    # Phase 1 (git + github) both start before Phase 2 (pr-origins).
    assert _idx(order, ("start", "git")) < _idx(order, ("start", "pr-origins"))
    assert _idx(order, ("start", "github")) < _idx(order, ("start", "pr-origins"))
    # Phase 1 both joined before Phase 2 starts.
    assert _idx(order, ("join", "git")) < _idx(order, ("start", "pr-origins"))
    assert _idx(order, ("join", "github")) < _idx(order, ("start", "pr-origins"))
    # LLM is strictly last and alone: analyze starts only after pr-origins joins.
    assert _idx(order, ("join", "pr-origins")) < _idx(order, ("start", "analyze"))

    # Closing results panel is present.
    assert "done in" in result.output


def test_no_llm_descriptions_drops_the_llm_phase(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[tuple[str, str]] = []
    stubs = _patch_crawlers(monkeypatch, order)
    monkeypatch.setattr(
        scan_mod, "_select_github_client", lambda *a, **k: _DummyClient()
    )

    result = CliRunner().invoke(whygraph_main, ["scan", "--no-llm-descriptions"])

    assert result.exit_code == 0, result.output
    # Two phases: structural + pr-origin recovery; no LLM phase.
    assert "Phase 1/2 · Structural crawl" in result.output
    assert "Phase 2/2 · PR-origin recovery" in result.output
    assert "· LLM descriptions" not in result.output
    assert stubs["analyze"].constructed == 0


def test_single_phase_when_remote_and_llm_disabled(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[tuple[str, str]] = []
    stubs = _patch_crawlers(monkeypatch, order)

    result = CliRunner().invoke(
        whygraph_main, ["scan", "--no-remote", "--no-llm-descriptions"]
    )

    assert result.exit_code == 0, result.output
    assert "Phase 1/1 · Structural crawl" in result.output
    assert "Phase 2" not in result.output
    assert stubs["github"].constructed == 0
    assert stubs["pr-origins"].constructed == 0
    assert stubs["analyze"].constructed == 0


class _Fake:
    """A minimal crawler stand-in for the pure results-panel unit test."""

    def __init__(
        self,
        name: str,
        *,
        error: BaseException | None = None,
        warning: str | None = None,
        summary: str | None = None,
    ) -> None:
        self.name = name
        self.error = error
        self.warning = warning
        self.summary = summary


def test_results_panel_is_defensive_and_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It renders failed / warned / skipped rows without raising (R10/R11)."""
    buf = io.StringIO()
    monkeypatch.setattr(scan_mod, "console", Console(file=buf, width=120))

    ran = [
        _Fake("git", summary="5 commits (5 new)"),
        _Fake("github", summary="2 PRs · 1 issues"),
        _Fake("analyze", error=RuntimeError("boom")),  # failed → ✗
        # pr-origins absent from `ran` → "— skipped" row
    ]
    codegraph = _Fake("codegraph", warning="CodeGraph refresh skipped — no binary")

    scan_mod._render_results_panel(
        ran=ran,
        codegraph_crawler=codegraph,
        db_path=Path("/repo/.whygraph/whygraph.db"),
        scan_log_path=Path("/repo/.whygraph/scan.log"),
        phase_timings={"Structural crawl": 4.1, "LLM descriptions": 128.0},
        total_elapsed=140.0,
    )

    out = buf.getvalue()
    assert "✗" in out  # failed analyze
    assert "⚠" in out  # codegraph warning
    assert "skipped" in out  # absent pr-origins
    assert "Scan log" in out  # R11: path row retained
    assert "done in" in out  # total elapsed in the title
