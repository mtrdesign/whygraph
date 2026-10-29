"""Tests for the portal's scan child, ``python -m whygraph.scan.child``.

Pins the phase *sequencing* - Phase 1 (git + GitHub, concurrent) -> Phase 2
(pr-origins) -> Phase 3 (authors, which needs Phase 2's rows) -> Phase 4
(analyze, the LLM long pole, last and alone) - and its numbering across
every flag combination, the JSONL event contract (``start`` first with
``phase_total``, one ``phase`` event per phase, ``task`` events, ``usage``
events, a closing ``result`` with the ``codegraph`` block), the exit codes,
the refusals (no ``WHYGRAPH_CONFIG_JSON``, a full scan with no
``whygraph_db``, a malformed config), and the stderr run log.

The crawlers are mostly stubbed, so no git / GitHub / LLM / CodeGraph work
runs in-process; one test runs a real child against a fixture repo.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator

import pytest

from whygraph import core
from whygraph.core.config import Config
from whygraph.core.usage import current_usage_sink, record_usage
from whygraph.db import ensure_initialized
from whygraph.db import engine as db_engine
from whygraph.scan import child
from whygraph.scan.json_progress import JsonUsageSink
from whygraph.services.codegraph import CodeGraphBootstrapError
from whygraph.services.llm import LlmError
from whygraph.services.llm.types import CompletionResponse


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("hello\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _events(text: str) -> list[dict]:
    """Parse every stdout line as JSON - a spliced or stray line raises."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temp git repo (no GitHub remote) as ``cwd``, with a fresh config cache."""
    root = _make_repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    monkeypatch.delenv("WHYGRAPH_CONFIG_JSON", raising=False)
    core._reset_config()
    try:
        yield root
    finally:
        core._reset_config()


@pytest.fixture
def isolated_db(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """A migrated DB outside the repo, named by the config the child is handed.

    ``core._config`` alone is not enough: the child refuses to run unless
    ``WHYGRAPH_CONFIG_JSON`` is set, so the env var names the same DB.
    """
    db_path = tmp_path / "history" / "whygraph.db"
    monkeypatch.setenv(
        "WHYGRAPH_CONFIG_JSON", json.dumps({"whygraph_db": str(db_path)})
    )
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    ensure_initialized()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()


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


_CLASSES = {
    "GitCrawler": "git",
    "GitHubCrawler": "github",
    "PROriginEnricher": "pr-origins",
    "AuthorResolver": "authors",
    "CodeGraphCrawler": "codegraph",
    "TagCrawler": "tags",
}


def _stub(
    name: str,
    order: list[tuple[str, str]] | None = None,
    *,
    fail: bool = False,
    on_init=None,  # noqa: ANN001
) -> type:
    """A crawler stand-in: records start / join order, reports a 2-step task."""

    class _Stub:
        constructed = 0
        last_kwargs: dict | None = None

        def __init__(self, progress: object, **kwargs: object) -> None:
            type(self).constructed += 1
            type(self).last_kwargs = kwargs
            self.name = name
            self.error: BaseException | None = None
            self.warning = None
            self.summary = f"{name} ok"
            self._progress = progress
            self._task = progress.add_task(name, total=2)  # type: ignore[attr-defined]
            self._thread: threading.Thread | None = None
            if on_init is not None:
                on_init(self)

        def _work(self) -> None:
            self._progress.update(self._task, advance=1)  # type: ignore[attr-defined]
            self._progress.update(self._task, advance=1)  # type: ignore[attr-defined]
            if fail:
                self.error = RuntimeError("boom")

        def start(self) -> None:
            if order is not None:
                order.append(("start", name))
            self._thread = threading.Thread(target=self._work)
            self._thread.start()

        def join(self, timeout: float | None = None) -> None:
            if order is not None:
                order.append(("join", name))
            assert self._thread is not None
            self._thread.join(timeout)

    return _Stub


def _patch_crawlers(
    monkeypatch: pytest.MonkeyPatch,
    order: list[tuple[str, str]] | None = None,
    *,
    failing: tuple[str, ...] = (),
) -> dict[str, type]:
    """Replace every crawler class the child touches with a stub."""
    stubs = {}
    for attr, name in _CLASSES.items():
        stubs[name] = _stub(name, order, fail=name in failing)
        monkeypatch.setattr(child, attr, stubs[name])
    # AnalyzeCrawler is imported lazily inside the child - patch its source.
    stubs["analyze"] = _stub("analyze", order, fail="analyze" in failing)
    monkeypatch.setattr("whygraph.scan.AnalyzeCrawler", stubs["analyze"])
    return stubs


def _with_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(child, "_select_github_client", lambda *a, **k: _DummyClient())


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, list[dict], str]:
    """``main(argv)`` -> (exit status, stdout events, stderr)."""
    status = child.main(list(argv))
    out, err = capsys.readouterr()
    return status, _events(out), err


def _idx(order: list[tuple[str, str]], event: tuple[str, str]) -> int:
    return order.index(event)


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #


def test_four_phases_run_in_order_with_llm_last(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    order: list[tuple[str, str]] = []
    stubs = _patch_crawlers(monkeypatch, order)
    _with_github(monkeypatch)
    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _DummyDescriptor)

    status, events, _ = _run(capsys)

    assert status == 0
    # All six crawlers constructed exactly once.
    for name in ("git", "github", "pr-origins", "authors", "analyze", "codegraph"):
        assert stubs[name].constructed == 1, name

    # Numbered phase events for all four phases.
    assert [(e["phase"], e["title"]) for e in events if e["type"] == "phase"] == [
        (1, "Structural crawl"),
        (2, "PR-origin recovery"),
        (3, "Author identity"),
        (4, "LLM descriptions"),
    ]

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

    assert events[-1]["type"] == "result" and events[-1]["status"] == "ok"


@pytest.mark.parametrize(
    ("flags", "remote", "titles"),
    [
        pytest.param(
            [],
            True,
            [
                "Structural crawl",
                "PR-origin recovery",
                "Author identity",
                "LLM descriptions",
            ],
            id="default",
        ),
        pytest.param(
            ["--skip-analyze"],
            True,
            ["Structural crawl", "PR-origin recovery", "Author identity"],
            id="skip-analyze",
        ),
        pytest.param(
            ["--no-remote"],
            False,
            ["Structural crawl", "Author identity", "LLM descriptions"],
            id="no-remote",
        ),
        pytest.param(
            ["--no-remote", "--skip-analyze"],
            False,
            ["Structural crawl", "Author identity"],
            id="no-remote-skip-analyze",  # the git-hook path
        ),
    ],
)
def test_phases_are_listed_and_numbered_across_flag_combinations(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    remote: bool,
    titles: list[str],
) -> None:
    """``phase_total`` is computed, so every combination is pinned - a stale
    count would mislabel every phase. The ``start`` event names every phase
    up front, in run order."""
    stubs = _patch_crawlers(monkeypatch)
    if remote:
        _with_github(monkeypatch)
    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _DummyDescriptor)

    status, events, _ = _run(capsys, *flags)

    assert status == 0
    assert events[0] == {"type": "start", "phase_total": len(titles), "phases": titles}
    assert [e for e in events if e["type"] == "phase"] == [
        {"type": "phase", "phase": i, "title": t} for i, t in enumerate(titles, 1)
    ]
    # Author resolution is local-only, so it runs under every combination.
    assert stubs["authors"].constructed == 1


def test_codegraph_only_start_event_sends_code_index(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WHYGRAPH_CONFIG_JSON", "{}")
    _patch_crawlers(monkeypatch)

    status, events, _ = _run(capsys, "--codegraph-only")

    assert status == 0
    assert events[0] == {"type": "start", "phase_total": 1, "phases": ["Code index"]}


# --------------------------------------------------------------------------- #
# The event stream and exit codes
# --------------------------------------------------------------------------- #


def test_skip_analyze_no_remote_emits_two_phases_and_a_result(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_crawlers(monkeypatch)

    status, events, _ = _run(capsys, "--skip-analyze", "--no-remote")

    assert status == 0
    assert events[0] == {
        "type": "start",
        "phase_total": 2,
        "phases": ["Structural crawl", "Author identity"],
    }
    assert events[-1]["type"] == "result"
    assert events[-1]["status"] == "ok"
    assert events[-1]["analyze_skipped"] == "--skip-analyze"
    assert {c["name"] for c in events[-1]["crawlers"]} == {
        "git",
        "authors",
        "codegraph",
    }
    # The stubs' tasks were reported, finishing at 2/2.
    tasks = [e for e in events if e["type"] == "task"]
    assert {t["name"] for t in tasks} == {"git", "authors", "codegraph"}
    assert all(
        [t for t in tasks if t["name"] == n][-1]["completed"] == 2
        for n in ("git", "authors", "codegraph")
    )


def test_failed_crawler_is_reported_in_result_and_exit_code(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_crawlers(monkeypatch, failing=("authors",))

    status, events, err = _run(capsys, "--skip-analyze", "--no-remote")

    assert status == 1
    assert events[-1]["type"] == "result"
    assert events[-1]["status"] == "failed"
    authors = next(c for c in events[-1]["crawlers"] if c["name"] == "authors")
    assert authors["status"] == "failed" and authors["error"] == "boom"
    assert "crawler 'authors' failed: boom" in err


def test_result_keeps_a_crawler_warning(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``scripts/smoke.sh`` reads the CodeGraph "symbolic link" warning here."""
    _patch_crawlers(monkeypatch)

    def warn(crawler: object) -> None:
        crawler.warning = "refusing a symbolic link"  # type: ignore[attr-defined]

    monkeypatch.setattr(child, "CodeGraphCrawler", _stub("codegraph", on_init=warn))

    status, events, _ = _run(capsys, "--skip-analyze", "--no-remote")

    assert status == 0
    codegraph = next(c for c in events[-1]["crawlers"] if c["name"] == "codegraph")
    assert codegraph["warning"] == "refusing a symbolic link"
    assert events[-1]["codegraph"] == {
        "status": "failed",
        "error": "refusing a symbolic link",
    }


@pytest.mark.parametrize(
    ("flags", "failing", "expected"),
    [
        ([], (), {"status": "ok", "files": None}),
        ([], ("codegraph",), {"status": "failed", "error": "boom"}),
        (["--no-codegraph"], (), {"status": "skipped"}),
    ],
)
def test_result_reports_the_codegraph_step(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    failing: tuple[str, ...],
    expected: dict,
) -> None:
    """``summary.codegraph`` (portal-hosted CodeGraph plan, 4.2a / 4.2c)."""
    _patch_crawlers(monkeypatch, failing=failing)

    _, events, _ = _run(capsys, "--skip-analyze", "--no-remote", *flags)

    assert events[-1]["type"] == "result"
    assert events[-1]["codegraph"] == expected


def test_result_counts_the_indexed_files(
    isolated_db: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sqlite3

    (repo / ".codegraph").mkdir()
    with sqlite3.connect(repo / ".codegraph" / "codegraph.db") as conn:
        conn.execute("CREATE TABLE files (path TEXT)")
        conn.executemany("INSERT INTO files VALUES (?)", [("a.py",), ("b.py",)])
    _patch_crawlers(monkeypatch)

    _, events, _ = _run(capsys, "--skip-analyze", "--no-remote")

    assert events[-1]["codegraph"] == {"status": "ok", "files": 2}


def test_a_full_scan_refreshes_codegraph_under_the_writer_lock(
    isolated_db: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``require_lock_dir=True`` always: no index write without the portal's lock."""
    stubs = _patch_crawlers(monkeypatch)

    status, _, _ = _run(capsys, "--skip-analyze", "--no-remote")

    assert status == 0
    assert stubs["codegraph"].last_kwargs == {
        "project_root": repo,
        "image": None,
        "require_lock_dir": True,
    }


def test_a_codegraph_only_scan_is_strict_under_the_writer_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WHYGRAPH_CONFIG_JSON", "{}")
    stubs = _patch_crawlers(monkeypatch)

    status, _, _ = _run(capsys, "--codegraph-only")

    assert status == 0
    assert stubs["codegraph"].last_kwargs == {
        "project_root": repo,
        "image": None,
        "strict": True,
        "require_lock_dir": True,
    }


def test_no_scan_log_is_written(
    isolated_db: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D10: the run log (stderr) replaces ``scan.log``, in the repo and beside the DB."""
    _patch_crawlers(monkeypatch)

    status, _, _ = _run(capsys, "--skip-analyze", "--no-remote")

    assert status == 0
    assert not (repo / ".whygraph" / "scan.log").exists()
    assert not (isolated_db.parent / "scan.log").exists()


# --------------------------------------------------------------------------- #
# --skip-analyze and the descriptor probe
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the "origin is not a GitHub remote" branch."""
    monkeypatch.setattr(
        "whygraph.services.github.GitHubClient.for_repository",
        classmethod(lambda cls, _repo: None),
    )


def test_skip_analyze_bypasses_the_descriptor_probe(
    isolated_db: Path,
    no_github: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With the flag set: no AnalyzeCrawler and no LlmDescriptor probe."""
    stubs = _patch_crawlers(monkeypatch)
    probe_calls = []

    class _RecordProbe:
        @classmethod
        def from_config(cls, cfg):  # noqa: ANN001
            probe_calls.append(cfg)
            return cls()

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _RecordProbe)

    status, events, _ = _run(capsys, "--skip-analyze")

    assert status == 0
    assert stubs["git"].constructed == 1
    assert stubs["analyze"].constructed == 0
    assert probe_calls == []  # descriptor probe bypassed entirely
    assert events[-1]["analyze_skipped"] == "--skip-analyze"


def test_skip_analyze_tolerates_broken_analyze_config(
    isolated_db: Path,
    no_github: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A broken `[analyze]` provider must not block a flag-scan."""
    stubs = _patch_crawlers(monkeypatch)

    class _AlwaysFails:
        @classmethod
        def from_config(cls, _cfg):  # noqa: ANN001
            raise LlmError("provider 'broken' is not registered")

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _AlwaysFails)

    status, _, _ = _run(capsys, "--skip-analyze")

    assert status == 0
    assert stubs["analyze"].constructed == 0


def test_a_failed_descriptor_probe_skips_the_llm_phase(
    isolated_db: Path,
    no_github: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stubs = _patch_crawlers(monkeypatch)

    class _AlwaysFails:
        @classmethod
        def from_config(cls, _cfg):  # noqa: ANN001
            raise LlmError("provider 'broken' is not registered")

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _AlwaysFails)

    status, events, _ = _run(capsys)

    assert status == 0
    assert stubs["analyze"].constructed == 0
    assert events[0]["phases"] == ["Structural crawl", "Author identity"]
    assert events[-1]["analyze_skipped"] == "provider 'broken' is not registered"


def test_scan_without_flag_still_constructs_analyze_crawler(
    isolated_db: Path,
    no_github: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression pin: default behaviour still runs the LLM phase."""
    stubs = _patch_crawlers(monkeypatch)
    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _DummyDescriptor)

    status, _, _ = _run(capsys)

    assert status == 0
    assert stubs["analyze"].constructed == 1


# --------------------------------------------------------------------------- #
# [scan].forge -> GitHub client
# --------------------------------------------------------------------------- #


def test_provider_off_returns_none_without_calling_for_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def _boom(cls, repo):  # noqa: ANN001  # pragma: no cover - must never run
        calls.append(repo)
        raise AssertionError("for_repository must not be called when provider=off")

    monkeypatch.setattr(
        "whygraph.services.github.GitHubClient.for_repository",
        classmethod(_boom),
    )

    assert child._select_github_client("off", repository=object()) is None
    assert calls == []


@pytest.mark.parametrize("provider", ["github", "auto"])
def test_provider_delegates_to_for_repository(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    sentinel = object()
    seen: list[object] = []

    def _resolve(cls, repo):  # noqa: ANN001
        seen.append(repo)
        return sentinel

    monkeypatch.setattr(
        "whygraph.services.github.GitHubClient.for_repository",
        classmethod(_resolve),
    )

    repo = object()
    assert child._select_github_client(provider, repository=repo) is sentinel
    assert seen == [repo]


# --------------------------------------------------------------------------- #
# Usage events
# --------------------------------------------------------------------------- #


class _MeteredAnalyze:
    """An ``AnalyzeCrawler`` stand-in that makes one metered call per commit.

    Like the real crawler, its thread runs in the context captured at
    construction (where the child binds the usage sink).
    """

    SHAS = ("a" * 40, "b" * 40)

    def __init__(self, progress: object, **_kwargs: object) -> None:
        self.name = "analyze"
        self.error: BaseException | None = None
        self.warning = None
        self.summary = "2 described"
        self._context = contextvars.copy_context()
        self._thread: threading.Thread | None = None

    def _work(self) -> None:
        for sha in self.SHAS:
            record_usage(
                "analyze",
                CompletionResponse(
                    text="d",
                    model="claude-served",
                    provider="anthropic",
                    input_tokens=100,
                    output_tokens=20,
                    cache_read_tokens=10,
                ),
                provider="anthropic",
                model_requested="claude-asked",
                subject=sha,
            )

    def start(self) -> None:
        self._thread = threading.Thread(target=self._context.run, args=(self._work,))
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        assert self._thread is not None
        self._thread.join(timeout)


def test_each_llm_call_is_reported_as_a_usage_event(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_crawlers(monkeypatch)
    monkeypatch.setattr("whygraph.scan.AnalyzeCrawler", _MeteredAnalyze)
    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _DummyDescriptor)

    status, events, _ = _run(capsys, "--no-remote")

    assert status == 0
    usage = [e for e in events if e["type"] == "usage"]
    assert [e["subject"] for e in usage] == list(_MeteredAnalyze.SHAS)
    assert usage[0] == {
        "type": "usage",
        "task": "analyze",
        "model_served": "claude-served",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 10,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
        "provider_cost_usd": None,
        "subject": "a" * 40,
        "duration_ms": None,
    }
    # Counts only: the runner, not the child, names the provider and model.
    assert "provider" not in usage[0] and "model_requested" not in usage[0]
    types = [e["type"] for e in events]
    assert types[-1] == "result"
    assert max(i for i, t in enumerate(types) if t == "usage") < len(types) - 1


@pytest.mark.parametrize(
    ("flags", "probe"),
    [
        (["--skip-analyze", "--no-remote"], "git"),
        (["--codegraph-only"], "codegraph"),
    ],
)
def test_the_usage_sink_is_bound_in_every_run(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    probe: str,
) -> None:
    """Bound before any crawler is built - no ``--managed-by-portal`` gate."""
    _patch_crawlers(monkeypatch)
    seen: list[object] = []
    attr = next(a for a, n in _CLASSES.items() if n == probe)
    monkeypatch.setattr(
        child, attr, _stub(probe, on_init=lambda _c: seen.append(current_usage_sink()))
    )

    status, _, _ = _run(capsys, *flags)

    assert status == 0
    assert len(seen) == 1 and isinstance(seen[0], JsonUsageSink)
    assert current_usage_sink() is None  # unbound again after the run


# --------------------------------------------------------------------------- #
# Refusals (D23)
# --------------------------------------------------------------------------- #


def test_no_config_json_exits_2_and_writes_nothing(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "history" / "whygraph.db"
    # Even a cached config naming a DB does not stand in for the env var.
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    stubs = _patch_crawlers(monkeypatch)

    status = child.main(["--skip-analyze", "--no-remote"])
    out, err = capsys.readouterr()

    assert status == 2
    assert out == ""
    assert err.count("\n") == 1 and "WHYGRAPH_CONFIG_JSON" in err
    assert stubs["git"].constructed == 0
    assert not db_path.exists()
    assert not (repo / ".whygraph").exists()


def test_a_full_scan_without_whygraph_db_exits_2_and_writes_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WHYGRAPH_CONFIG_JSON", json.dumps({"scan": {"forge": "off"}}))
    stubs = _patch_crawlers(monkeypatch)

    status = child.main(["--skip-analyze", "--no-remote"])
    out, err = capsys.readouterr()

    assert status == 2
    assert out == ""
    assert err.count("\n") == 1 and "whygraph_db" in err
    assert stubs["git"].constructed == 0
    assert not (repo / ".whygraph").exists()


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]"])
def test_a_malformed_config_fails_with_one_line(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    monkeypatch.setenv("WHYGRAPH_CONFIG_JSON", payload)

    status = child.main([])
    out, err = capsys.readouterr()

    assert status == 1
    assert out == ""
    assert err.count("\n") == 1 and "WHYGRAPH_CONFIG_JSON" in err
    assert payload not in err


def test_unknown_flags_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    for flag in ("--managed-by-portal", "--progress"):
        with pytest.raises(SystemExit) as exc:
            child.main([flag])
        assert exc.value.code == 2
    capsys.readouterr()


def test_codegraph_only_and_github_only_are_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        child.main(["--codegraph-only", "--github-only"])
    assert exc.value.code == 2
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# --github-only (the runner's ``forge`` trigger)
# --------------------------------------------------------------------------- #


def test_github_only_runs_the_forge_crawl_and_the_tags_only(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stubs = _patch_crawlers(monkeypatch)
    _with_github(monkeypatch)

    status, events, _ = _run(capsys, "--github-only")

    assert status == 0
    assert events[0] == {"type": "start", "phase_total": 1, "phases": ["Forge crawl"]}
    assert stubs["github"].constructed == 1 and stubs["tags"].constructed == 1
    for skipped in ("git", "pr-origins", "authors", "codegraph", "analyze"):
        assert stubs[skipped].constructed == 0, skipped
    result = events[-1]
    assert result["type"] == "result" and result["status"] == "ok"
    assert {c["name"] for c in result["crawlers"]} == {"github", "tags"}
    assert result["analyze_skipped"] == "--github-only"
    assert result["codegraph"] == {"status": "skipped"}


def test_github_only_without_a_forge_still_mirrors_tags(
    isolated_db: Path,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real tag crawler: the tag table refreshes, no commit is crawled."""
    from sqlmodel import select

    from whygraph.db import get_session
    from whygraph.db.models.commit import Commit as CommitRow
    from whygraph.db.models.tag import Tag as TagRow

    _git(repo, "config", "tag.gpgsign", "false")
    _git(repo, "tag", "v1")

    status, events, _ = _run(capsys, "--github-only")

    assert status == 0
    assert [c["name"] for c in events[-1]["crawlers"]] == ["tags"]
    assert events[-1]["crawlers"][0]["summary"] == "1 tag"
    with get_session() as session:
        assert [t.name for t in session.exec(select(TagRow)).all()] == ["v1"]
        assert session.exec(select(CommitRow)).all() == []

    _git(repo, "tag", "-d", "v1")
    _git(repo, "tag", "v2")
    status, _, _ = _run(capsys, "--github-only")

    assert status == 0
    with get_session() as session:
        assert [t.name for t in session.exec(select(TagRow)).all()] == ["v2"]


def test_github_only_fails_when_the_forge_crawl_fails(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_crawlers(monkeypatch, failing=("github",))
    _with_github(monkeypatch)

    status, events, err = _run(capsys, "--github-only")

    assert status == 1
    assert events[-1]["status"] == "failed"
    assert "crawler 'github' failed" in err


# --------------------------------------------------------------------------- #
# --codegraph-only with the real crawler
# --------------------------------------------------------------------------- #


@pytest.fixture
def codegraph_only_repo(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A linked project's child: config without ``whygraph_db``, a lock dir."""
    monkeypatch.setenv("WHYGRAPH_CONFIG_JSON", "{}")
    monkeypatch.setenv("WHYGRAPH_CODEGRAPH_LOCK_DIR", str(tmp_path / "locks"))
    return repo


@pytest.fixture
def child_logging() -> Iterator[None]:
    """Clear the ``whygraph`` logger's handlers, then restore the logger.

    The test calls :func:`child._configure_logging` itself, in its body, so
    the handler binds the ``sys.stderr`` that ``capsys`` reads.
    """
    logger = logging.getLogger("whygraph")
    saved = (list(logger.handlers), logger.level, logger.propagate)
    for h in saved[0]:
        logger.removeHandler(h)
    try:
        yield
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
        for h in saved[0]:
            logger.addHandler(h)
        logger.setLevel(saved[1])
        logger.propagate = saved[2]


def test_codegraph_only_scan_creates_no_whygraph_db(
    codegraph_only_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    child_logging: None,
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        "whygraph.scan.codegraph_crawler.refresh_codegraph_index",
        lambda root, **kw: calls.append(root),
    )
    child._configure_logging()

    status, events, err = _run(capsys, "--codegraph-only")

    assert status == 0
    assert calls == [codegraph_only_repo]
    assert events[0] == {"type": "start", "phase_total": 1, "phases": ["Code index"]}
    assert events[-1]["type"] == "result" and events[-1]["status"] == "ok"
    assert [c["name"] for c in events[-1]["crawlers"]] == ["codegraph"]
    assert events[-1]["codegraph"] == {"status": "ok", "files": None}
    # No WhyGraph DB and no scan.log: the run log is stderr.
    assert not (codegraph_only_repo / ".whygraph").exists()
    assert "INFO" in err and "mode=codegraph-only" in err


def test_codegraph_only_scan_fails_on_codegraph_error(
    codegraph_only_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(root: Path, **kw: object) -> None:
        raise CodeGraphBootstrapError("codegraph exited with status 3")

    monkeypatch.setattr("whygraph.scan.codegraph_crawler.refresh_codegraph_index", boom)

    status, events, err = _run(capsys, "--codegraph-only")

    assert status == 1
    last = events[-1]
    assert last["type"] == "result" and last["status"] == "failed"
    assert last["crawlers"][0]["status"] == "failed"
    assert "status 3" in last["crawlers"][0]["error"]
    assert last["codegraph"]["status"] == "failed"
    assert "status 3" in err
    assert not (codegraph_only_repo / ".whygraph").exists()


# --------------------------------------------------------------------------- #
# The run log (stderr)
# --------------------------------------------------------------------------- #


def test_configure_logging_is_idempotent(child_logging: None) -> None:
    child._configure_logging()
    child._configure_logging()

    logger = logging.getLogger("whygraph")
    assert len(logger.handlers) == 1
    (handler,) = logger.handlers
    assert isinstance(handler, logging.StreamHandler)
    assert handler.level == logging.INFO
    assert logger.level == logging.INFO
    assert logger.propagate is False
    # Nothing on the root logger.
    assert handler not in logging.getLogger().handlers


def test_main_does_not_configure_logging(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the ``__main__`` block does, so in-process runs add no handlers."""
    _patch_crawlers(monkeypatch)
    logger = logging.getLogger("whygraph")
    before = list(logger.handlers)

    _run(capsys, "--skip-analyze", "--no-remote")

    assert logger.handlers == before


def test_the_run_log_has_start_and_end_lines_and_no_httpx(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    child_logging: None,
) -> None:
    _patch_crawlers(monkeypatch)

    def chatty(_crawler: object) -> None:
        logging.getLogger("httpx").info("HTTP Request: POST https://example.test/v1")
        logging.getLogger("whygraph.scan.git_crawler").info("git crawl begins")

    monkeypatch.setattr(child, "GitCrawler", _stub("git", on_init=chatty))
    child._configure_logging()

    status, _, err = _run(capsys, "--skip-analyze", "--no-remote")

    assert status == 0
    lines = err.splitlines()
    assert any(
        "INFO" in line
        and "scan starting: mode=full flags=--skip-analyze --no-remote" in line
        for line in lines
    )
    assert any("scan finished: status=ok elapsed=" in line for line in lines)
    assert "git crawl begins" in err
    assert "httpx" not in err and "HTTP Request" not in err


# --------------------------------------------------------------------------- #
# End to end: a real child scan of a fixture repo
# --------------------------------------------------------------------------- #


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    src = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))
    env.pop("WHYGRAPH_CONFIG_JSON", None)
    env.update(extra)
    return env


def test_real_child_scan_of_a_fixture_repo_emits_valid_jsonl(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    db_path = tmp_path / "history" / "w.db"

    proc = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "whygraph.scan.child",
            "--no-codegraph",
            "--skip-analyze",
            "--no-remote",
        ],
        cwd=repo,
        env=_child_env(
            WHYGRAPH_CONFIG_JSON=json.dumps(
                {"scan": {"forge": "off"}, "whygraph_db": str(db_path)}
            )
        ),
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr
    events = _events(proc.stdout)  # every line valid JSON, nothing else on stdout
    assert events[0] == {
        "type": "start",
        "phase_total": 2,
        "phases": ["Structural crawl", "Author identity"],
    }
    phases = [e for e in events if e["type"] == "phase"]
    assert len(phases) == events[0]["phase_total"]
    assert events[-1]["type"] == "result" and events[-1]["status"] == "ok"
    assert events[-1]["codegraph"] == {"status": "skipped"}
    git_tasks = [e for e in events if e["type"] == "task" and e["name"] == "git"]
    assert git_tasks and git_tasks[-1]["completed"] == git_tasks[-1]["total"]
    # The history landed where the config said; the run log is stderr.
    assert db_path.is_file()
    assert "scan starting: mode=full" in proc.stderr
    assert "scan finished: status=ok" in proc.stderr
    # The config came from the env: nothing was written into the checkout.
    assert not (repo / "whygraph.toml").exists()
    assert not (repo / ".whygraph").exists()
