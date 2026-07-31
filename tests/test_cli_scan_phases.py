"""Tests for ``whygraph scan``'s phased orchestration and output.

Pins the phase *sequencing* — Phase 1 (git + GitHub, concurrent) → Phase 2
(pr-origins) → Phase 3 (authors, which needs Phase 2's rows) → Phase 4
(analyze, the LLM long pole, last and alone) — plus the numbered phase
headers across every flag combination and the closing results panel. The
crawlers are stubbed with recording stand-ins so no git / GitHub / LLM /
CodeGraph work actually runs; only the orchestrator's ordering and
rendering is exercised.
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
        for n in ("git", "github", "pr-origins", "authors", "analyze", "codegraph")
    }
    monkeypatch.setattr(scan_mod, "GitCrawler", stubs["git"])
    monkeypatch.setattr(scan_mod, "GitHubCrawler", stubs["github"])
    monkeypatch.setattr(scan_mod, "PROriginEnricher", stubs["pr-origins"])
    monkeypatch.setattr(scan_mod, "AuthorResolver", stubs["authors"])
    monkeypatch.setattr(scan_mod, "CodeGraphCrawler", stubs["codegraph"])
    # AnalyzeCrawler is imported lazily inside scan_cmd — patch its source.
    monkeypatch.setattr("whygraph.scan.AnalyzeCrawler", stubs["analyze"])
    return stubs


def _idx(order: list[tuple[str, str]], event: tuple[str, str]) -> int:
    return order.index(event)


def test_four_phases_run_in_order_with_llm_last(
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
    # All six crawlers constructed exactly once.
    for name in ("git", "github", "pr-origins", "authors", "analyze", "codegraph"):
        assert stubs[name].constructed == 1, name

    # Numbered headers for all four phases.
    assert "Phase 1/4 · Structural crawl" in result.output
    assert "Phase 2/4 · PR-origin recovery" in result.output
    assert "Phase 3/4 · Author identity" in result.output
    assert "Phase 4/4 · LLM descriptions" in result.output

    # CodeGraph is a background task: started first, joined last.
    assert order[0] == ("start", "codegraph")
    assert order[-1] == ("join", "codegraph")

    # Phase 1 (git + github) both start before Phase 2 (pr-origins).
    assert _idx(order, ("start", "git")) < _idx(order, ("start", "pr-origins"))
    assert _idx(order, ("start", "github")) < _idx(order, ("start", "pr-origins"))
    # Phase 1 both joined before Phase 2 starts.
    assert _idx(order, ("join", "git")) < _idx(order, ("start", "pr-origins"))
    assert _idx(order, ("join", "github")) < _idx(order, ("start", "pr-origins"))
    # Author resolution runs after PR-origin recovery: an address can appear
    # only in the on_default_branch=0 rows Phase 2 writes.
    assert _idx(order, ("join", "pr-origins")) < _idx(order, ("start", "authors"))
    # LLM is strictly last and alone.
    assert _idx(order, ("join", "authors")) < _idx(order, ("start", "analyze"))

    # Closing results panel is present.
    assert "done in" in result.output


@pytest.mark.parametrize(
    ("flags", "remote", "expected", "absent"),
    [
        pytest.param(
            [],
            True,
            [
                "Phase 1/4 · Structural crawl",
                "Phase 2/4 · PR-origin recovery",
                "Phase 3/4 · Author identity",
                "Phase 4/4 · LLM descriptions",
            ],
            [],
            id="default",
        ),
        pytest.param(
            ["--skip-analyze"],
            True,
            [
                "Phase 1/3 · Structural crawl",
                "Phase 2/3 · PR-origin recovery",
                "Phase 3/3 · Author identity",
            ],
            ["· LLM descriptions"],
            id="skip-analyze",
        ),
        pytest.param(
            ["--no-remote"],
            False,
            [
                "Phase 1/3 · Structural crawl",
                "Phase 2/3 · Author identity",
                "Phase 3/3 · LLM descriptions",
            ],
            ["· PR-origin recovery"],
            id="no-remote",
        ),
        pytest.param(
            ["--no-remote", "--skip-analyze"],
            False,
            [
                "Phase 1/2 · Structural crawl",
                "Phase 2/2 · Author identity",
            ],
            ["· PR-origin recovery", "· LLM descriptions"],
            id="no-remote-skip-analyze",  # the git-hook path
        ),
    ],
)
def test_phase_numbering_across_flag_combinations(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: list[str],
    remote: bool,
    expected: list[str],
    absent: list[str],
) -> None:
    """``phase_total`` is computed, so every combination is pinned — a stale
    count mislabels every header (e.g. two "Phase 3/3" rules)."""
    order: list[tuple[str, str]] = []
    stubs = _patch_crawlers(monkeypatch, order)
    if remote:
        monkeypatch.setattr(
            scan_mod, "_select_github_client", lambda *a, **k: _DummyClient()
        )
    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _DummyDescriptor)

    result = CliRunner().invoke(whygraph_main, ["scan", *flags])

    assert result.exit_code == 0, result.output
    for header in expected:
        assert header in result.output
    for header in absent:
        assert header not in result.output
    # Author resolution is local-only, so it runs under every combination.
    assert stubs["authors"].constructed == 1
    # `n` never exceeds `phase_total`.
    total = len(expected)
    assert f"Phase {total + 1}/" not in result.output


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
        # pr-origins and authors absent from `ran` → "— skipped" rows
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
    assert "Author identity" in out  # the row exists even when absent from `ran`
    assert "skipped" in out  # absent pr-origins
    assert "Scan log" in out  # R11: path row retained
    assert "done in" in out  # total elapsed in the title
