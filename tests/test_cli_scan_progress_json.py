"""Tests for ``whygraph scan --progress json`` and ``WHYGRAPH_CONFIG_JSON``.

Covers the JSONL event contract (``start`` first with ``phase_total``, one
``phase`` event per phase, throttled ``task`` events, a closing ``result``),
the lock that keeps lines from concurrent crawler threads from splicing, the
hidden ``--managed-by-portal`` no-op, and the env-sourced config that wins
over a checkout's ``whygraph.toml``.
"""

from __future__ import annotations

import contextvars
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from whygraph import core
from whygraph.cli import main as whygraph_main
from whygraph.cli.commands import scan as scan_mod
from whygraph.cli.console import console
from whygraph.core.config import Config, ConfigError
from whygraph.core.usage import record_usage
from whygraph.db import ensure_initialized
from whygraph.db import engine as db_engine
from whygraph.scan import JsonProgress
from whygraph.services.llm.types import CompletionResponse


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


def _events(text: str) -> list[dict]:
    """Parse every stdout line as JSON - a spliced or stray line raises."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# JsonProgress
# --------------------------------------------------------------------------- #


class _RecordingStream:
    """Records each ``write`` call whole, so a splice would be visible."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1


def test_json_progress_emits_add_task_and_final_update() -> None:
    stream = _RecordingStream()
    progress = JsonProgress(stream=stream)

    task = progress.add_task("git", total=4)
    progress.update(task, advance=4, description="git done")

    events = _events("".join(stream.writes))
    assert events[0] == {
        "type": "task",
        "name": "git",
        "completed": 0,
        "total": 4,
        "description": "git",
    }
    assert events[-1] == {
        "type": "task",
        "name": "git",
        "completed": 4,
        "total": 4,
        "description": "git done",
    }


def test_json_progress_throttles_updates_and_flush_sends_latest() -> None:
    stream = _RecordingStream()
    progress = JsonProgress(stream=stream, throttle_sec=3600)
    task = progress.add_task("git", total=100)

    for _ in range(50):
        progress.update(task, advance=1)

    # add_task + nothing else: every update fell inside the throttle window.
    assert len(stream.writes) == 1
    progress.flush()
    latest = _events(stream.writes[-1])[0]
    assert latest["completed"] == 50
    # Idempotent: nothing left to flush.
    progress.flush()
    assert len(stream.writes) == 2


def test_json_progress_always_emits_finish_and_total_change() -> None:
    stream = _RecordingStream()
    progress = JsonProgress(stream=stream, throttle_sec=3600)
    task = progress.add_task("codegraph", total=None)

    progress.update(task, total=1)  # structural change - not throttled
    progress.update(task, advance=1)  # finishing - not throttled

    events = _events("".join(stream.writes))
    assert [(e["completed"], e["total"]) for e in events] == [
        (0, None),
        (0, 1),
        (1, 1),
    ]


def test_json_progress_lines_from_concurrent_threads_never_splice() -> None:
    stream = _RecordingStream()
    progress = JsonProgress(stream=stream, throttle_sec=0)
    n_threads, n_updates = 8, 200

    def work(idx: int) -> None:
        task = progress.add_task(f"crawler-{idx}", total=n_updates)
        for _ in range(n_updates):
            progress.update(task, advance=1)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert stream.writes, "expected events"
    for chunk in stream.writes:
        # One write == exactly one complete JSON line.
        assert chunk.endswith("\n") and chunk.count("\n") == 1
        json.loads(chunk)
    assert stream.flushes == len(stream.writes)
    # Every crawler's final state made it out.
    finals = {e["name"]: e["completed"] for e in _events("".join(stream.writes))}
    assert finals == {f"crawler-{i}": n_updates for i in range(n_threads)}


def test_json_progress_swallows_a_closed_stream() -> None:
    stream = io.StringIO()
    progress = JsonProgress(stream=stream)
    stream.close()

    task = progress.add_task("git", total=1)  # must not raise
    progress.update(task, advance=1)


# --------------------------------------------------------------------------- #
# CLI wiring (crawlers stubbed)
# --------------------------------------------------------------------------- #


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
    owner = "acme"
    name = "widgets"
    pull_requests: tuple = ()
    issues: tuple = ()


class _DummyDescriptor:
    @classmethod
    def from_config(cls, _cfg: object) -> "_DummyDescriptor":
        return cls()


def _stub(name: str, *, fail: bool = False) -> type:
    """A crawler stand-in that registers a task and reports progress."""

    class _Stub:
        def __init__(self, progress: object, **_kwargs: object) -> None:
            self.name = name
            self.error: BaseException | None = None
            self.warning = None
            self.summary = f"{name} ok"
            self._progress = progress
            self._task = progress.add_task(name, total=2)  # type: ignore[attr-defined]
            self._thread: threading.Thread | None = None

        def _work(self) -> None:
            self._progress.update(self._task, advance=1)  # type: ignore[attr-defined]
            self._progress.update(self._task, advance=1)  # type: ignore[attr-defined]
            if fail:
                self.error = RuntimeError("boom")

        def start(self) -> None:
            self._thread = threading.Thread(target=self._work)
            self._thread.start()

        def join(self, timeout: float | None = None) -> None:
            assert self._thread is not None
            self._thread.join(timeout)

    return _Stub


def _patch_crawlers(
    monkeypatch: pytest.MonkeyPatch, *, failing: tuple[str, ...] = ()
) -> None:
    names = {
        "GitCrawler": "git",
        "GitHubCrawler": "github",
        "PROriginEnricher": "pr-origins",
        "AuthorResolver": "authors",
        "CodeGraphCrawler": "codegraph",
    }
    for attr, name in names.items():
        monkeypatch.setattr(scan_mod, attr, _stub(name, fail=name in failing))
    monkeypatch.setattr(
        "whygraph.scan.AnalyzeCrawler", _stub("analyze", fail="analyze" in failing)
    )


def test_skip_analyze_no_remote_emits_two_phases_and_a_result(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_crawlers(monkeypatch)

    result = CliRunner().invoke(
        whygraph_main,
        ["scan", "--progress", "json", "--skip-analyze", "--no-remote"],
    )

    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    assert events[0] == {
        "type": "start",
        "phase_total": 2,
        "phases": ["Structural crawl", "Author identity"],
    }
    assert [e["title"] for e in events if e["type"] == "phase"] == [
        "Structural crawl",
        "Author identity",
    ]
    assert [e["phase"] for e in events if e["type"] == "phase"] == [1, 2]
    assert events[-1]["type"] == "result"
    assert events[-1]["status"] == "ok"
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
    # No Rich output leaks onto either stream in this mode.
    assert "Phase" not in result.stderr
    assert "whygraph scan" not in result.stderr


def test_all_phases_match_phase_total(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_crawlers(monkeypatch)
    monkeypatch.setattr(
        scan_mod, "_select_github_client", lambda *a, **k: _DummyClient()
    )
    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _DummyDescriptor)

    result = CliRunner().invoke(whygraph_main, ["scan", "--progress", "json"])

    assert result.exit_code == 0, result.output
    events = _events(result.stdout)
    assert events[0]["phase_total"] == 4
    assert [e["phase"] for e in events if e["type"] == "phase"] == [1, 2, 3, 4]


def test_failed_crawler_is_reported_in_result_and_exit_code(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_crawlers(monkeypatch, failing=("authors",))

    result = CliRunner().invoke(
        whygraph_main,
        ["scan", "--progress", "json", "--skip-analyze", "--no-remote"],
    )

    assert result.exit_code == 1
    events = _events(result.stdout)
    assert events[-1]["type"] == "result"
    assert events[-1]["status"] == "failed"
    authors = next(c for c in events[-1]["crawlers"] if c["name"] == "authors")
    assert authors["status"] == "failed" and authors["error"] == "boom"


def test_console_quiet_is_restored_after_a_json_scan(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_crawlers(monkeypatch)
    assert console.quiet is False

    CliRunner().invoke(
        whygraph_main,
        ["scan", "--progress", "json", "--skip-analyze", "--no-remote"],
    )

    assert console.quiet is False


def test_default_scan_output_is_not_json(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_crawlers(monkeypatch)

    result = CliRunner().invoke(
        whygraph_main, ["scan", "--skip-analyze", "--no-remote"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""
    assert "Phase 1/2" in result.stderr


def test_managed_by_portal_is_accepted_and_hidden(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_crawlers(monkeypatch)

    help_out = CliRunner().invoke(whygraph_main, ["scan", "--help"]).output
    assert "--managed-by-portal" not in help_out
    assert "--progress" in help_out

    result = CliRunner().invoke(
        whygraph_main,
        [
            "scan",
            "--progress",
            "json",
            "--managed-by-portal",
            "--skip-analyze",
            "--no-remote",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _events(result.stdout)[0]["type"] == "start"


class _MeteredAnalyze:
    """An ``AnalyzeCrawler`` stand-in that makes one metered call per commit.

    Like the real crawler, its thread runs in the context captured at
    construction (where ``whygraph scan`` binds the usage sink).
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


def _metered_scan(monkeypatch: pytest.MonkeyPatch, *flags: str) -> list[dict]:
    _patch_crawlers(monkeypatch)
    monkeypatch.setattr("whygraph.scan.AnalyzeCrawler", _MeteredAnalyze)
    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _DummyDescriptor)
    result = CliRunner().invoke(whygraph_main, ["scan", "--no-remote", *flags])
    assert result.exit_code == 0, result.output
    if "--progress" not in flags:
        assert result.stdout.strip() == ""
        return []
    return _events(result.stdout)


def test_a_managed_scan_reports_each_llm_call_as_a_usage_event(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _metered_scan(monkeypatch, "--progress", "json", "--managed-by-portal")

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


def test_an_unmanaged_scan_records_no_usage(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _metered_scan(monkeypatch, "--progress", "json")

    assert events[-1]["type"] == "result"
    assert not [e for e in events if e["type"] == "usage"]
    # A headless (non-JSON) scan has no sink either; nothing reaches stdout.
    _metered_scan(monkeypatch)


# --------------------------------------------------------------------------- #
# End to end: a real scan of a fixture repo in a child process
# --------------------------------------------------------------------------- #


def _child_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    src = str(Path(__file__).resolve().parent.parent / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))
    env.pop("WHYGRAPH_CONFIG_JSON", None)
    env.update(extra)
    return env


def test_real_scan_of_a_fixture_repo_emits_valid_jsonl(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "whygraph",
            "scan",
            "--progress",
            "json",
            "--managed-by-portal",
            "--skip-analyze",
            "--no-remote",
            "--no-codegraph",
        ],
        cwd=repo,
        env=_child_env(WHYGRAPH_CONFIG_JSON=json.dumps({"scan": {"forge": "off"}})),
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
    git_tasks = [e for e in events if e["type"] == "task" and e["name"] == "git"]
    assert git_tasks and git_tasks[-1]["completed"] == git_tasks[-1]["total"]
    # The config came from the env: nothing was written into the checkout.
    assert not (repo / "whygraph.toml").exists()


# --------------------------------------------------------------------------- #
# WHYGRAPH_CONFIG_JSON
# --------------------------------------------------------------------------- #


@pytest.fixture
def fresh_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("WHYGRAPH_CONFIG_JSON", raising=False)
    core._reset_config()
    try:
        yield
    finally:
        core._reset_config()


def test_config_json_wins_over_whygraph_toml(
    repo: Path, monkeypatch: pytest.MonkeyPatch, fresh_config: None
) -> None:
    (repo / "whygraph.toml").write_text("[analyze]\nmax_workers = 3\n")
    assert core.get_config().analyze.max_workers == 3

    core._reset_config()
    monkeypatch.setenv(
        "WHYGRAPH_CONFIG_JSON", json.dumps({"analyze": {"max_workers": 7}})
    )

    assert core.get_config().analyze.max_workers == 7


def test_config_json_without_a_toml_and_relative_paths_use_the_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch, fresh_config: None
) -> None:
    monkeypatch.setenv(
        "WHYGRAPH_CONFIG_JSON",
        json.dumps({"scan": {"forge": "off"}, "whygraph_db": "x/w.db"}),
    )

    config = core.get_config()

    assert config.scan_forge == "off"
    assert config.whygraph_db == (repo / "x" / "w.db").resolve()


def test_empty_config_json_falls_back_to_the_toml(
    repo: Path, monkeypatch: pytest.MonkeyPatch, fresh_config: None
) -> None:
    (repo / "whygraph.toml").write_text("[analyze]\nmax_workers = 3\n")
    monkeypatch.setenv("WHYGRAPH_CONFIG_JSON", "  ")

    assert core.get_config().analyze.max_workers == 3


@pytest.mark.parametrize("payload", ["{not json", "[1, 2]", '"str"'])
def test_bad_config_json_raises_config_error_without_echoing_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch, fresh_config: None, payload: str
) -> None:
    monkeypatch.setenv("WHYGRAPH_CONFIG_JSON", payload)

    with pytest.raises(ConfigError, match="WHYGRAPH_CONFIG_JSON") as exc:
        core.get_config()

    assert payload not in str(exc.value)
