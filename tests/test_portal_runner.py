"""Tests for the portal scan runner - :mod:`whygraph.portal.runner` (plan section 4.6).

The child scanner is ``tests/fixtures/fake_scan.py``, injected through
``WHYGRAPH_SCAN_CMD`` (the runner ``shlex.split``s it and appends its own
``--progress json --managed-by-portal <flags>``). The fake records each
invocation's argv / env / cwd to a JSONL file and can be held while a
"hold" file exists, which is how the tests keep a scan running.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`) imported from test_portal_app

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import anyio
import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlmodel import select

from conftest import builtin_org_id
from test_portal_app import (  # noqa: F401 -- `env` is a fixture
    BASE_URL,
    CLIENT_HEADER,
    _git,
    add_local,
    env,
    init_project,
    make_repo,
    manual_ctx,
    seed_codegraph,
)
from test_portal_mcp import _free_port
from whygraph.cli.commands.portal import GRACEFUL_SHUTDOWN_SEC
from whygraph.core.config import Config
from whygraph.core.context import use_project
from whygraph.db import ensure_initialized
from whygraph.db import get_session as project_session
from whygraph.db.models import Commit
from whygraph.portal import db as portal_db
from whygraph.portal.app import PortalServer, create_portal_app
from whygraph.portal.estimate import (
    CHARS_PER_LINE,
    CHARS_PER_TOKEN,
    OUTPUT_TOKENS_PER_COMMIT,
    SYNTHESIS_INPUT_TOKENS,
    CommitSize,
    estimate_tokens,
    render_estimate,
)
from whygraph.portal import routes as routes_mod
from whygraph.portal import runner as runner_mod
from whygraph.portal.models import Project, ScanRun
from whygraph.portal.runner import (
    LOG_TAIL_BYTES,
    MAX_EVENT_LINE,
    TRIGGER_PRECEDENCE,
    RunnerUnavailable,
    ScanRunner,
    SourceNotAllowed,
    _event_line,
    _insert_run,
    _Pending,
    _read_frames,
    _recover_queued,
    _update_queued,
    child_env,
    merge_trigger,
    redactor,
    resolve_analyze,
    scan_argv,
    scan_flags,
    sweep_token_files,
    write_token_file,
)

FAKE_SCAN = Path(__file__).parent / "fixtures" / "fake_scan.py"
TERMINAL = ("ok", "failed", "interrupted", "cancelled")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def scanner(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """Point the runner at the fake scanner; ``configure(**opts)`` changes its options."""
    record = env.tmp / "scans.jsonl"
    hold = env.tmp / "hold"

    def configure(**opts: Any) -> None:
        parts = [sys.executable, str(FAKE_SCAN), "--record", str(record)]
        parts += ["--hold", str(hold)]
        for key, value in opts.items():
            flag = f"--{key.replace('_', '-')}"
            parts += [flag] if value is True else [flag, str(value)]
        monkeypatch.setenv("WHYGRAPH_SCAN_CMD", shlex.join(parts))

    def calls() -> list[dict]:
        if not record.is_file():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]

    configure()
    ns = SimpleNamespace(record=record, hold=hold, configure=configure, calls=calls)
    yield ns
    hold.unlink(missing_ok=True)


def runner_flags(call: dict) -> list[str]:
    """The part of a recorded argv the runner added (from ``--progress`` on)."""
    argv = call["argv"]
    return argv[argv.index("--progress") :]


def wait_for(predicate: Callable[[], Any], timeout: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.03)


def runs(client: TestClient, slug: str) -> list[dict]:
    response = client.get(f"/api/projects/{slug}/scans")
    assert response.status_code == 200, response.text
    return response.json()["runs"]


def run_by_id(client: TestClient, slug: str, run_id: int) -> dict:
    return next(r for r in runs(client, slug) if r["id"] == run_id)


def wait_run(client: TestClient, slug: str, run_id: int, timeout: float = 20.0) -> dict:
    return wait_for(
        lambda: (r := run_by_id(client, slug, run_id))["status"] in TERMINAL and r,
        timeout,
    )


def wait_idle(client: TestClient, slug: str) -> list[dict]:
    return wait_for(
        lambda: (
            (rs := runs(client, slug))
            and all(r["status"] in TERMINAL for r in rs)
            and rs
        )
    )


def scan(client: TestClient, slug: str, **body: Any) -> int:
    response = client.post(f"/api/projects/{slug}/scans", json=body or None)
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def client_for(runner: ScanRunner | None = None) -> TestClient:
    app = create_portal_app(port=8765, runner=runner)
    return TestClient(app, base_url=BASE_URL, headers=CLIENT_HEADER)


@pytest.fixture
def portal(env: SimpleNamespace, scanner: SimpleNamespace) -> Iterator[TestClient]:
    """A portal past setup, with the fake scanner configured."""
    with client_for() as client:
        assert (
            client.post("/api/portal/setup", json={"display_name": "Tess"}).status_code
            == 201
        )
        yield client


def local_project(client: TestClient, env: SimpleNamespace, name: str) -> Path:
    root = make_repo(env.shared, name)
    seed_codegraph(root)
    slug = add_local(client, root)["project"]["slug"]
    assert slug == name
    assert init_project(client, slug)["initialized"] is True
    return root


def legacy_github_project(env: SimpleNamespace, name: str) -> int:
    """A local-mode GitHub clone of an earlier build, initialized; its id."""
    dest = make_repo(env.data / "repos", name)
    seed_codegraph(dest)
    with use_project(manual_ctx(dest, slug=name)):
        ensure_initialized()
    with portal_db.get_session() as session:
        project = Project(
            org_id=builtin_org_id(session),
            slug=name,
            name=name,
            source="github",
            root=f"repos/{name}",
            remote_url=f"https://github.com/acme/{name}",
            initialized_at="2026-10-01T00:00:00+00:00",
        )
        session.add(project)
        session.flush()
        return project.id


def commit(root: Path, text: str) -> str:
    (root / "sample.py").write_text(text)
    _git(root, "add", "sample.py")
    _git(root, "commit", "-q", "-m", f"change {text[:10]}")
    return _git(root, "rev-parse", "HEAD").strip()


def first_scan(client: TestClient, slug: str) -> dict:
    """Run the structure-only first scan to completion (so later ones keep their trigger)."""
    run = wait_run(client, slug, scan(client, slug))
    assert run["status"] == "ok" and run["trigger"] == "initial"
    return run


# ---------------------------------------------------------------------------
# Unit: trigger semantics, argv, env, redaction, estimate arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trigger", "analyze", "flags"),
    [
        ("initial", None, ["--skip-analyze"]),
        ("hook", None, ["--skip-analyze", "--no-remote"]),
        ("poll", None, ["--skip-analyze"]),
        ("sync", None, ["--skip-analyze"]),
        ("manual", None, []),
        ("manual", True, []),
        ("manual", False, ["--skip-analyze"]),
        ("describe", None, []),
    ],
)
def test_trigger_to_flags(trigger: str, analyze: bool | None, flags: list[str]) -> None:
    resolved = resolve_analyze(trigger, analyze)
    assert scan_flags(trigger, resolved) == flags
    assert resolved is (flags == [])
    argv = scan_argv(
        trigger, resolved, {"WHYGRAPH_SCAN_CMD": "whygraph scan --no-codegraph"}
    )
    assert argv == [
        "whygraph",
        "scan",
        "--no-codegraph",
        "--progress",
        "json",
        "--managed-by-portal",
        *flags,
    ]


def test_default_argv_runs_this_interpreter() -> None:
    argv = scan_argv("hook", False, {})
    assert argv[:4] == [sys.executable, "-m", "whygraph", "scan"]
    assert argv[4:] == [
        "--progress",
        "json",
        "--managed-by-portal",
        "--skip-analyze",
        "--no-remote",
    ]


def test_trigger_precedence() -> None:
    assert TRIGGER_PRECEDENCE == (
        "hook",
        "poll",
        "reconcile",
        "push",
        "sync",
        "initial",
        "manual",
        "describe",
    )
    assert merge_trigger("hook", "manual") == "manual"
    assert merge_trigger("manual", "hook") == "manual"
    assert merge_trigger("describe", "manual") == "describe"
    assert merge_trigger("poll", "sync") == "sync"
    assert merge_trigger("initial", "sync") == "initial"
    assert merge_trigger("push", "reconcile") == "push"
    assert merge_trigger("reconcile", "poll") == "reconcile"
    assert merge_trigger("push", "sync") == "sync"


def test_child_env_allowlist_and_injected_secrets(tmp_path: Path) -> None:
    portal_env = {
        "PATH": "/bin",
        "HOME": "/home/me",
        "LC_ALL": "C",
        "SSL_CERT_FILE": "/etc/cert.pem",
        "HTTPS_PROXY": "http://proxy:3128",
        "ANTHROPIC_API_KEY": "sk-portal-env",
        "GH_TOKEN": "gh-portal-env",
        "GITHUB_TOKEN": "gh2-portal-env",
        "OPENAI_API_KEY": "sk-openai-env",
    }
    config = Config.from_dict(
        {
            "llm": {
                "model": "anthropic/claude-opus-4-7",
                "anthropic": {"api_key": "sk-proj"},
            },
            "scan": {"forge": "auto", "token": "ghp_project"},
        },
        tmp_path,
    )
    layer = {"scan": {"forge": "auto"}}
    env, secrets = child_env(
        config, layer, source="local", analyze=True, environ=portal_env
    )
    assert env["PATH"] == "/bin" and env["LC_ALL"] == "C"
    assert env["SSL_CERT_FILE"] == "/etc/cert.pem"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert json.loads(env["WHYGRAPH_CONFIG_JSON"]) == layer
    assert env["GH_TOKEN"] == "ghp_project"
    assert env["ANTHROPIC_API_KEY"] == "sk-proj"
    assert "OPENAI_API_KEY" not in env and "GITHUB_TOKEN" not in env
    assert "WHYGRAPH_GIT_TOKEN" not in env
    assert sorted(secrets) == ["ghp_project", "sk-proj"]

    assert "GIT_CONFIG_GLOBAL" not in env and "GIT_CONFIG_NOSYSTEM" not in env

    # A GitHub (production) project: no personal access token at all - not
    # for gh, not for git - and git reads no global or system config.
    structure_only, secrets = child_env(
        config,
        layer,
        source="github",
        analyze=False,
        environ={**portal_env, "WHYGRAPH_GITHUB_URL": "http://127.0.0.1:9999"},
    )
    assert "ANTHROPIC_API_KEY" not in structure_only
    assert "GH_TOKEN" not in structure_only
    assert "WHYGRAPH_GIT_TOKEN" not in structure_only
    assert structure_only["GIT_CONFIG_GLOBAL"] == os.devnull
    assert structure_only["GIT_CONFIG_NOSYSTEM"] == "1"
    assert structure_only["WHYGRAPH_GITHUB_URL"] == "http://127.0.0.1:9999"
    assert secrets == []
    no_url, _ = child_env(
        config, layer, source="github", analyze=False, environ=portal_env
    )
    assert "WHYGRAPH_GITHUB_URL" not in no_url
    # Production: the token file's path, never a token value.
    token_file = tmp_path / "runs" / "7.token"
    with_file, secrets = child_env(
        config,
        layer,
        source="github",
        analyze=False,
        environ=portal_env,
        token_file=token_file,
    )
    assert with_file["WHYGRAPH_GITHUB_TOKEN_FILE"] == str(token_file)
    assert "GH_TOKEN" not in with_file and "WHYGRAPH_GIT_TOKEN" not in with_file
    assert secrets == []

    bare, secrets = child_env(
        Config.from_dict({}, tmp_path),
        {},
        source="local",
        analyze=True,
        environ=portal_env,
    )
    assert "GH_TOKEN" not in bare and "ANTHROPIC_API_KEY" not in bare
    assert secrets == []


def test_redactor_replaces_secrets_with_hints() -> None:
    redact = redactor(["ghp_abcdefgh1234", "sk-zzzz9876"])
    out = redact("token ghp_abcdefgh1234 and key sk-zzzz9876 end")
    assert "ghp_abcdefgh1234" not in out and "sk-zzzz9876" not in out
    assert out.count("1234") == 1 and out.count("9876") == 1


def test_redactor_matches_a_partly_masked_token_it_never_injected() -> None:
    """Spike #7: ``gh auth status`` prints most of an installation token."""
    minted = "ghs_1227_eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJJdjEifQ.c2lnbmF0dXJl"
    shown = minted[:-14] + "*" * 14
    redact = redactor(["sk-zzzz9876"])
    out = redact(f"Logged in (GH_TOKEN) token: {shown} and sk-zzzz9876")
    assert minted[:16] not in out and "*" * 14 not in out
    assert out == "Logged in (GH_TOKEN) token: ghs_*** and …9876"
    assert redactor([])(json.dumps({"line": shown})) == '{"line": "ghs_***"}'


def test_the_redactor_learns_new_values() -> None:
    redact = redactor(["sk-zzzz9876"])
    first, second = "ghs_" + "a" * 36 + "1111", "ghs_" + "b" * 36 + "2222"
    redact.learn(first)
    redact.learn(second, "")
    out = redact(f"{first} {second} sk-zzzz9876")
    assert out == "…1111 …2222 …9876"


def test_token_file_is_0600_and_never_read_torn(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "3.token"
    path.parent.mkdir()
    values = ["ghs_" + c * 4000 for c in "abcdef"]
    write_token_file(path, values[0])
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == values[0]

    stop = threading.Event()
    seen: set[str] = set()

    def reader() -> None:
        while not stop.is_set():
            seen.add(path.read_text())

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(300):
            write_token_file(path, values[i % len(values)])
    finally:
        stop.set()
        thread.join()
    assert seen <= set(values) and len(seen) > 1
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["3.token"]


def test_leftover_token_files_are_swept_at_start(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    runs_dir = env.data / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for name in ("4.token", "5.token.0a1b2c.tmp", "4.log", "4.jsonl"):
        (runs_dir / name).write_text("x")
    assert sweep_token_files(env.tmp) == 0  # nothing there
    with client_for():
        assert sorted(p.name for p in runs_dir.iterdir()) == ["4.jsonl", "4.log"]


def test_estimate_arithmetic() -> None:
    sizes = [
        CommitSize(files_changed=1, insertions=10, deletions=5),  # small single file
        CommitSize(files_changed=1, insertions=5000, deletions=0),  # capped
        CommitSize(files_changed=3, insertions=2000, deletions=0),  # chunked
        CommitSize(files_changed=2, insertions=10, deletions=10),  # small multi
        CommitSize(files_changed=50, insertions=9000, deletions=9000),  # bulk stub
    ]
    result = estimate_tokens(sizes, max_diff_chars=50_000, large_commit_file_count=30)
    chars = 15 * CHARS_PER_LINE + 50_000 + 2000 * CHARS_PER_LINE + 20 * CHARS_PER_LINE
    assert result.commits == 4
    assert result.large_commits == 1
    assert result.synthesis_calls == 1
    assert result.input_tokens == chars // CHARS_PER_TOKEN + SYNTHESIS_INPUT_TOKENS
    assert result.output_tokens == 4 * OUTPUT_TOKENS_PER_COMMIT
    # Pin the constants too, so a silent change shows up here.
    assert (CHARS_PER_LINE, CHARS_PER_TOKEN) == (60, 4)
    assert (OUTPUT_TOKENS_PER_COMMIT, SYNTHESIS_INPUT_TOKENS) == (300, 1_500)
    assert result.input_tokens == 44_525

    priced = render_estimate(result, provider="anthropic", model="claude-opus-4-7")
    usd = (44_525 * 5.0 + 1_200 * 25.0) / 1_000_000
    assert priced["cost"]["usd"] == round(usd, 4)
    assert priced["cost"]["low"] == round(usd * 0.5, 4)
    assert priced["cost"]["high"] == round(usd * 1.5, 4)
    assert priced["tokens"]["input_range"] == {"low": 22262.5, "high": 66787.5}
    assert priced["upper_bound"] is True
    unpriced = render_estimate(result, provider="ollama", model="llama3")
    assert unpriced["cost"] is None and unpriced["tokens"]["input"] == 44_525


# ---------------------------------------------------------------------------
# Runner through the portal app
# ---------------------------------------------------------------------------


def test_read_frames_skips_a_line_longer_than_a_chunk(tmp_path: Path) -> None:
    """Fix pass 2: a > 256 KiB line used to stall the stream forever."""
    path = tmp_path / "run.jsonl"
    big = json.dumps(
        {"type": "result", "crawlers": [{"name": "git", "error": "x" * 300_000}]}
    )
    start = json.dumps({"type": "start", "phase_total": 2})
    phase = json.dumps({"type": "phase", "phase": 2})
    path.write_text(f"{start}\n{big}\n{phase}\n")

    seen: list[dict] = []
    pos = 0
    for _ in range(4):
        frames, pos = _read_frames(path, pos)
        seen += [json.loads(f.split("data: ", 1)[1]) for f in frames]
    assert [f["type"] for f in seen] == ["start", "oversized", "phase"]
    assert seen[1]["bytes"] == len(big)
    assert pos == path.stat().st_size


def test_read_frames_waits_for_an_incomplete_long_line(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    path.write_bytes(b"x" * 300_000)  # still being written: no newline yet
    assert _read_frames(path, 0) == ([], 0)


def test_oversized_child_lines_are_written_as_a_placeholder() -> None:
    text = json.dumps({"type": "result", "error": "x" * MAX_EVENT_LINE})
    line = _event_line(text, json.loads(text))
    assert len(line) < 200 and line.endswith(b"\n")
    assert json.loads(line) == {
        "type": "oversized",
        "original_type": "result",
        "bytes": len(text),
    }
    small = json.dumps({"type": "phase", "phase": 1})
    assert _event_line(small, json.loads(small)) == small.encode() + b"\n"


def test_first_scan_is_initial_and_argv_per_trigger(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    root = local_project(portal, env, "demo")
    head = _git(root, "rev-parse", "HEAD").strip()

    # Whatever asks, the first scan records `initial` and spends nothing.
    run_id = scan(portal, "demo", trigger="describe")
    run = wait_run(portal, "demo", run_id)
    assert (run["status"], run["trigger"], run["analyze"]) == ("ok", "initial", False)
    assert run["summary"]["exit_code"] == 0
    with portal_db.get_session() as session:
        project = session.exec(select(Project).where(Project.slug == "demo")).one()
        assert project.last_scanned_head == head and project.last_scan_at

    for body, trigger, flags in [
        ({"trigger": "hook"}, "hook", ["--skip-analyze", "--no-remote"]),
        ({}, "manual", []),
        ({"trigger": "manual", "analyze": False}, "manual", ["--skip-analyze"]),
        ({"trigger": "describe"}, "describe", []),
    ]:
        run = wait_run(portal, "demo", scan(portal, "demo", **body))
        assert (run["status"], run["trigger"]) == ("ok", trigger)
        assert run["requested_by"] == (None if trigger == "hook" else 1)
    calls = scanner.calls()
    assert [runner_flags(c) for c in calls] == [
        ["--progress", "json", "--managed-by-portal", *flags]
        for flags in (
            ["--skip-analyze"],
            ["--skip-analyze", "--no-remote"],
            [],
            ["--skip-analyze"],
            [],
        )
    ]
    assert all(Path(c["cwd"]) == root for c in calls)


def test_hook_scans_are_refused_outside_local_mode(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    portal.app.state.portal.mode = "production"
    response = portal.post("/api/projects/demo/scans", json={"trigger": "hook"})
    assert response.status_code == 403
    assert response.json() == {
        "error": "hook scans exist only in local mode",
        "code": "hook_local_only",
    }
    # (and production holds no local folder at all: the source policy)
    manual = portal.post("/api/projects/demo/scans", json={"trigger": "manual"})
    assert manual.status_code == 409 and manual.json()["code"] == "source_not_allowed"
    assert scanner.calls() == []
    portal.app.state.portal.mode = "local"
    first_scan(portal, "demo")
    run = wait_run(portal, "demo", scan(portal, "demo", trigger="hook"))
    assert run["trigger"] == "hook"


def test_single_flight_and_union_coalescing(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    first_scan(portal, "demo")
    scanner.hold.touch()
    running = scan(portal, "demo", trigger="hook")
    wait_for(lambda: len(scanner.calls()) == 2)  # the held child is up
    assert run_by_id(portal, "demo", running)["status"] == "running"

    pending = scan(portal, "demo", trigger="hook")
    assert pending != running
    assert scan(portal, "demo", trigger="manual") == pending  # coalesced: same run id
    assert scan(portal, "demo", trigger="hook") == pending
    queued = run_by_id(portal, "demo", pending)
    assert (queued["status"], queued["trigger"], queued["analyze"]) == (
        "queued",
        "manual",
        True,
    )
    assert queued["requested_by"] == 1  # first explicit requester kept
    assert len(scanner.calls()) == 2  # single-flight: only the held run started

    scanner.hold.unlink()
    assert wait_run(portal, "demo", pending)["status"] == "ok"
    calls = scanner.calls()
    assert len(calls) == 3  # the burst became exactly one follow-up
    assert runner_flags(calls[1])[3:] == ["--skip-analyze", "--no-remote"]
    assert runner_flags(calls[2])[3:] == []  # the merged run analyzes and is remote


def test_global_cap_of_two(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    for name in ("a1", "a2", "a3"):
        local_project(portal, env, name)
    scanner.hold.touch()
    ids = {name: scan(portal, name) for name in ("a1", "a2", "a3")}
    wait_for(lambda: len(scanner.calls()) == 2)
    time.sleep(0.3)
    statuses = sorted(run_by_id(portal, n, i)["status"] for n, i in ids.items())
    assert statuses == ["queued", "running", "running"]
    assert len(scanner.calls()) == 2
    scanner.hold.unlink()
    for name, run_id in ids.items():
        assert wait_run(portal, name, run_id)["status"] == "ok"
    assert len(scanner.calls()) == 3


def test_failed_scan_and_stderr_flood(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    scanner.configure(stderr_bytes=3_000_000)
    run = wait_run(portal, "demo", scan(portal, "demo"))
    assert run["status"] == "ok"  # the drained pipe never blocks the child
    log = env.data / "runs" / f"{run['id']}.log"
    assert log.stat().st_size > 3_000_000

    scanner.configure(exit=1)
    run = wait_run(portal, "demo", scan(portal, "demo"))
    assert run["status"] == "failed" and run["summary"]["exit_code"] == 1
    assert run["summary"]["status"] == "failed"  # the child's result event


def test_child_env_passes_allowlist_not_portal_credentials(
    portal: TestClient,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project(portal, env, "demo")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/cert.pem")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-portal-env")
    monkeypatch.setenv("GH_TOKEN", "ghp_portal_env")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_portal_env2")
    # The portal's own database settings (the URL is set by `env`); the
    # password file must hold the real password, the portal reads it too.
    password = env.tmp / "postgres.password"
    password.write_text(make_url(os.environ["WHYGRAPH_DATABASE_URL"]).password or "")
    monkeypatch.setenv("WHYGRAPH_DATABASE_PASSWORD_FILE", str(password))
    first_scan(portal, "demo")
    wait_run(portal, "demo", scan(portal, "demo", trigger="manual"))
    for call in scanner.calls():
        child = call["env"]
        assert child["SSL_CERT_FILE"] == "/etc/ssl/cert.pem"
        assert child["HTTPS_PROXY"] == "http://proxy.example:3128"
        assert child["GIT_TERMINAL_PROMPT"] == "0"
        assert "WHYGRAPH_CONFIG_JSON" in child
        assert not any(k.endswith("_API_KEY") for k in child)
        assert "GH_TOKEN" not in child and "GITHUB_TOKEN" not in child
        assert "WHYGRAPH_DATABASE_URL" not in child
        assert "WHYGRAPH_DATABASE_PASSWORD_FILE" not in child
        config = json.loads(child["WHYGRAPH_CONFIG_JSON"])
        assert config["whygraph_db"].endswith(".whygraph/whygraph.db")


def test_injected_secrets_never_reach_run_files(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    gh_token = "ghp_SECRETtoken000abcd"
    llm_key = "sk-ant-SECRETkey000wxyz"
    response = portal.put(
        "/api/projects/demo/config",
        json={
            "config": {
                "scan": {"forge": "auto"},
                "llm": {"model": "anthropic/claude-opus-4-7"},
            },
            "secrets": {"github_token": gh_token, "llm": {"anthropic": llm_key}},
        },
    )
    assert response.status_code == 200, response.text
    first_scan(portal, "demo")

    scanner.configure(echo_env="GH_TOKEN")
    gh_run = wait_run(portal, "demo", scan(portal, "demo", trigger="hook"))
    scanner.configure(echo_env="ANTHROPIC_API_KEY")
    key_run = wait_run(portal, "demo", scan(portal, "demo", trigger="manual"))

    calls = scanner.calls()
    assert calls[1]["env"]["GH_TOKEN"] == gh_token  # it really was injected
    assert "ANTHROPIC_API_KEY" not in calls[1]["env"]  # structure-only: no LLM key
    assert calls[2]["env"]["ANTHROPIC_API_KEY"] == llm_key
    for run, secret in ((gh_run, gh_token), (key_run, llm_key)):
        jsonl = (env.data / "runs" / f"{run['id']}.jsonl").read_text()
        log = (env.data / "runs" / f"{run['id']}.log").read_text()
        for text in (jsonl, log):
            assert secret not in text
            assert f"value=…{secret[-4:]}" in text
        for line in jsonl.splitlines():
            json.loads(line)  # the jsonl stays pure JSON lines


def test_a_partly_masked_token_never_reaches_run_files(
    portal: TestClient,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pattern catches what exact-value redaction cannot (spike #7)."""
    local_project(portal, env, "demo")
    first_scan(portal, "demo")
    minted = "ghs_1227_eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJJdjEifQ.c2lnbmF0dXJl"
    # LC_* passes the allowlist, so the child sees (and prints) it.
    monkeypatch.setenv("LC_WHYGRAPH_PROBE", minted[:-14] + "*" * 14)
    scanner.configure(echo_env="LC_WHYGRAPH_PROBE")
    run = wait_run(portal, "demo", scan(portal, "demo", trigger="hook"))
    jsonl = (env.data / "runs" / f"{run['id']}.jsonl").read_text()
    log = (env.data / "runs" / f"{run['id']}.log").read_text()
    for text in (jsonl, log):
        assert minted[:16] not in text and "**********" not in text
        assert "value=ghs_***" in text
    for line in jsonl.splitlines():
        json.loads(line)


def test_events_sse_replays_then_follows(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    scanner.hold.touch()
    run_id = scan(portal, "demo")
    wait_for(lambda: len(scanner.calls()) == 1)
    wait_for(lambda: (env.data / "runs" / f"{run_id}.jsonl").stat().st_size > 0)
    threading.Timer(0.5, scanner.hold.unlink).start()

    response = portal.get(f"/api/projects/demo/scans/{run_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    frames = _frames(response.text)
    data = [f for f in frames if f.get("event") is None]
    assert [json.loads(f["data"])["type"] for f in data] == [
        "start",
        "phase",
        "phase",
        "result",
    ]
    offsets = [int(f["id"]) for f in data]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    end = frames[-1]
    assert end["event"] == "end"
    assert json.loads(end["data"])["status"] == "ok"

    # Resume after the second frame: only the rest is replayed.
    resumed = portal.get(
        f"/api/projects/demo/scans/{run_id}/events",
        headers={"Last-Event-ID": str(offsets[1])},
    )
    rest = [json.loads(f["data"])["type"] for f in _frames(resumed.text) if "id" in f]
    assert rest == ["phase", "result", "end"]
    assert portal.get("/api/projects/demo/scans/999/events").status_code == 404


async def _raw_get(app: Any, path: str, headers: list[tuple[bytes, bytes]]) -> dict:
    """One GET straight through the ASGI app, with header bytes as given.

    The TestClient re-encodes header values as UTF-8, so a raw latin-1
    byte (what a real client can send) only arrives this way.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8765"), (b"x-whygraph-client", b"1")]
        + headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8765),
    }
    sent: list[dict] = []
    requested = False

    async def receive() -> dict:
        nonlocal requested
        if requested:  # the stream's disconnect listener: never disconnects
            await anyio.sleep_forever()
        requested = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return {"status": sent[0]["status"], "text": body.decode()}


@pytest.mark.parametrize("last_event_id", [b"\xb2", b"1\xb9", b"-5", b"abc"])
def test_a_malformed_last_event_id_replays_from_the_start(
    portal: TestClient,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    last_event_id: bytes,
) -> None:
    """Fix pass 2: ``"²".isdigit()`` is true and ``int("²")`` raised a 500."""
    local_project(portal, env, "demo")
    run_id = scan(portal, "demo")
    wait_run(portal, "demo", run_id)
    assert portal.portal is not None
    response = portal.portal.call(
        _raw_get,
        portal.app,
        f"/api/projects/demo/scans/{run_id}/events",
        [(b"last-event-id", last_event_id)],
    )
    assert response["status"] == 200
    types = [
        json.loads(f["data"])["type"] for f in _frames(response["text"]) if "id" in f
    ]
    assert types[0] == "start" and types[-1] == "end"


def _frames(text: str) -> list[dict]:
    frames = []
    for block in text.split("\n\n"):
        fields: dict = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            fields[key] = value
        if fields:
            frames.append(fields)
    return frames


def test_restart_interrupts_running_and_requeues_queued(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        local_project(client, env, "demo")
        local_project(client, env, "gone")
    with portal_db.get_session() as session:
        demo = session.exec(select(Project).where(Project.slug == "demo")).one()
        gone = session.exec(select(Project).where(Project.slug == "gone")).one()
        gone.root = str(env.tmp / "unmounted")
        session.add(gone)
        stale = ScanRun(project_id=demo.id, trigger="manual", status="running")
        waiting = ScanRun(
            project_id=demo.id, trigger="hook", status="queued", analyze=False
        )
        orphan = ScanRun(
            project_id=gone.id, trigger="hook", status="queued", analyze=False
        )
        # A second queued row for the same project folds into the first.
        extra = ScanRun(
            project_id=demo.id, trigger="manual", status="queued", analyze=True
        )
        session.add_all([stale, waiting, orphan, extra])
        session.flush()
        for row in (waiting, orphan, extra):
            row.events_path, row.log_path = f"runs/{row.id}.jsonl", f"runs/{row.id}.log"
        ids = (stale.id, waiting.id, orphan.id, extra.id)

    with client_for() as client:
        assert run_by_id(client, "demo", ids[0])["status"] == "interrupted"
        requeued = wait_run(client, "demo", ids[1])
        assert requeued["status"] == "ok"
        assert (requeued["trigger"], requeued["analyze"]) == ("manual", True)
        merged = run_by_id(client, "demo", ids[3])
        assert merged["status"] == "cancelled"
        assert merged["summary"] == {"merged_into": ids[1]}
        with portal_db.get_session() as session:
            assert session.get(ScanRun, ids[2]).status == "cancelled"
    assert [runner_flags(c)[3:] for c in scanner.calls()] == [[]]


def test_recover_queued_reads_the_persisted_scan_flag(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        local_project(client, env, "demo")
    with portal_db.get_session() as session:
        demo_id = session.exec(select(Project).where(Project.slug == "demo")).one().id
    run_id = _insert_run(demo_id, "sync", "initial", False, None)
    _update_queued(
        _Pending(
            run_id=run_id,
            project_id=demo_id,
            kind="sync",
            trigger="initial",
            analyze=False,
            requested_by=None,
            scan_requested=True,
        )
    )
    (spec,) = _recover_queued()
    assert spec.scan_requested is True
    with portal_db.get_session() as session:
        assert json.loads(session.get(ScanRun, run_id).summary) == {
            "scan_requested": True
        }


def test_delete_refuses_while_a_scan_request_is_in_flight(
    portal: TestClient,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix pass 2: ``is_busy`` alone raced a request that had no row yet."""
    local_project(portal, env, "demo")
    entered, release = threading.Event(), threading.Event()
    real = runner_mod._has_ok_scan

    def _slow_has_ok_scan(project_id: int) -> bool:
        entered.set()
        release.wait(10)
        return real(project_id)

    monkeypatch.setattr(runner_mod, "_has_ok_scan", _slow_has_ok_scan)
    posted: list[httpx.Response] = []
    poster = threading.Thread(
        target=lambda: posted.append(portal.post("/api/projects/demo/scans"))
    )
    poster.start()
    assert entered.wait(5)
    refused = portal.delete("/api/projects/demo")
    release.set()
    poster.join(10)
    assert refused.status_code == 409, refused.text
    assert posted[0].status_code == 202
    wait_idle(portal, "demo")
    assert portal.get("/api/projects/demo").status_code == 200


def test_scan_request_refused_while_a_removal_runs(
    portal: TestClient,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_project(portal, env, "demo")
    entered, release = threading.Event(), threading.Event()
    real = routes_mod.sync_hooks

    def _slow_sync_hooks(root: Path, names):  # noqa: ANN001
        entered.set()
        release.wait(10)
        return real(root, names)

    monkeypatch.setattr(routes_mod, "sync_hooks", _slow_sync_hooks)
    deleted: list[httpx.Response] = []
    deleter = threading.Thread(
        target=lambda: deleted.append(portal.delete("/api/projects/demo"))
    )
    deleter.start()
    assert entered.wait(5)
    refused = portal.post("/api/projects/demo/scans")
    release.set()
    deleter.join(10)
    assert refused.status_code == 409, refused.text
    assert "being removed" in refused.json()["error"]
    assert deleted[0].status_code == 200, deleted[0].text
    assert scanner.calls() == []
    with portal_db.get_session() as session:
        assert session.exec(select(ScanRun)).all() == []


def test_removing_a_project_deletes_its_run_files(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    """``runs/<id>.jsonl``, ``.log`` and ``.token`` go with the row (M2d-2 section 0.2 #17)."""
    local_project(portal, env, "demo")
    local_project(portal, env, "other")
    run_id = scan(portal, "demo")
    other_id = scan(portal, "other")
    wait_idle(portal, "demo")
    wait_idle(portal, "other")
    runs_dir = env.data / "runs"
    (runs_dir / f"{run_id}.token").write_text("left over")
    mine = [runs_dir / f"{run_id}{ext}" for ext in (".jsonl", ".log", ".token")]
    others = [runs_dir / f"{other_id}{ext}" for ext in (".jsonl", ".log")]
    assert all(p.is_file() for p in mine + others)

    response = portal.delete("/api/projects/demo")
    assert response.status_code == 200, response.text
    assert not any(p.exists() for p in mine)
    assert all(p.is_file() for p in others)


def test_run_files_outside_the_runs_dir_are_never_deleted(
    env: SimpleNamespace,
) -> None:
    outside = env.tmp / "precious.log"
    outside.write_text("keep")
    (env.data / "runs").mkdir(parents=True, exist_ok=True)
    escape = env.data / "runs" / "link"
    escape.symlink_to(env.tmp, target_is_directory=True)
    runs = [
        (1, "../precious.log", "runs/../../precious.log"),
        (2, "runs/link/precious.log", None),
        (3, str(outside), None),
    ]
    assert runner_mod.remove_run_files(env.data, runs) == 0
    assert outside.read_text() == "keep"


class FakeClock:
    """An ``async sleep`` the test advances by hand."""

    def __init__(self) -> None:
        self.requested: list[float] = []
        self.gate = threading.Event()

    async def sleep(self, seconds: float) -> None:
        self.requested.append(seconds)
        while not self.gate.is_set():
            await anyio.sleep(0.01)
        self.gate.clear()

    def advance(self) -> None:
        self.gate.set()


def test_poll_loop_runs_the_catch_up(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    clock = FakeClock()
    with client_for(ScanRunner(sleep=clock.sleep)) as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        local = local_project(client, env, "loc")
        first_scan(client, "loc")
        assert clock.requested == [900]

        clock.advance()  # tick 1: HEAD matches, so nothing is queued
        wait_for(lambda: len(clock.requested) == 2)
        assert len(runs(client, "loc")) == 1

        commit(local, "a commit the hook never reported\n")
        clock.advance()  # tick 2: the local HEAD moved -> one catch-up hook scan
        wait_for(lambda: len(runs(client, "loc")) == 2)
        catch_up = wait_idle(client, "loc")[0]
        assert (catch_up["trigger"], catch_up["status"]) == ("hook", "ok")
        assert runner_flags(scanner.calls()[-1])[3:] == [
            "--skip-analyze",
            "--no-remote",
        ]
        assert client.get("/api/projects").json()["projects"][0]["stale"] is None


def test_a_local_github_row_is_refused_on_every_request_path(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    """Routes and internal callers alike (M2d-2 section 0.2 #16)."""
    runner = ScanRunner(sleep=FakeClock().sleep)
    with client_for(runner) as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        project_id = legacy_github_project(env, "gh")
        refused = client.post("/api/projects/gh/scans")
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "source_not_allowed"

        async def internal() -> None:
            await runner._request(
                project_id,
                kind="sync",
                trigger="poll",
                analyze=False,
                requested_by=None,
                scan_requested=False,
            )

        with pytest.raises(SourceNotAllowed, match="no longer supported"):
            client.portal.call(internal)
        client.portal.call(runner.catch_up)  # skips it, no error
    with portal_db.get_session() as session:
        assert session.exec(select(ScanRun.id)).all() == []
    assert scanner.calls() == []


def test_a_queued_run_of_a_local_github_row_fails_without_scanning(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    """A run an older build queued is requeued at start, then refused."""
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
    project_id = legacy_github_project(env, "gh")
    run_id = _insert_run(project_id, "sync", "poll", False, None)
    with client_for(ScanRunner(sleep=FakeClock().sleep)):
        row = _wait_row(run_id)
    assert row.status == "failed"
    assert "no longer supported" in json.loads(row.summary)["error"]
    assert scanner.calls() == []


def test_catch_up_on_start_only_when_head_moved(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    with client_for() as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        root = local_project(client, env, "demo")
        first_scan(client, "demo")
    with client_for() as client:  # HEAD unchanged: nothing queued
        time.sleep(0.3)
        assert len(runs(client, "demo")) == 1
    commit(root, "committed while the portal was down\n")
    commit(root, "and another one\n")
    with client_for(ScanRunner(sleep=FakeClock().sleep)) as client:
        summary = client.get("/api/projects").json()["projects"][0]
        assert summary["stale"] in ({"commits_behind": 2}, None)  # None once rescanned
        rs = wait_idle(client, "demo")
        assert len(rs) == 2 and rs[0]["trigger"] == "hook" and rs[0]["status"] == "ok"
        assert client.get("/api/projects").json()["projects"][0]["stale"] is None


def test_stale_reports_commits_behind(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    root = local_project(portal, env, "demo")
    assert portal.get("/api/projects/demo").json()["stale"] is None  # never scanned
    first_scan(portal, "demo")
    commit(root, "one\n")
    commit(root, "two\n")
    commit(root, "three\n")
    assert portal.get("/api/projects/demo").json()["stale"] == {"commits_behind": 3}


def test_scan_estimate_endpoint(portal: TestClient, env: SimpleNamespace) -> None:
    root = local_project(portal, env, "demo")
    rows = [
        ("a" * 40, 1, 10, 5, None, None),
        ("b" * 40, 2, 10, 10, None, None),
        ("c" * 40, 40, 100, 100, None, None),  # over large_commit_file_count
        ("d" * 40, 1, 10, 0, "already described", None),
        ("e" * 40, 0, 0, 0, None, None),  # empty: never described
        ("f" * 40, 1, 10, 0, None, "refs/pull/7/head"),  # PR-origin: never described
        ("0" * 40, 1, 10, 0, None, "feature/x"),  # a local branch still counts
    ]
    with use_project(manual_ctx(root)), project_session() as session:
        for sha, files, ins, dels, desc, ref in rows:
            session.add(
                Commit(
                    sha=sha,
                    parent_shas="",
                    author_name="T",
                    author_email="t@example.com",
                    authored_at="2026-01-01T00:00:00+00:00",
                    committed_at="2026-01-01T00:00:00+00:00",
                    subject="s",
                    body="",
                    files_changed=files,
                    insertions=ins,
                    deletions=dels,
                    scanned_at="2026-01-01T00:00:00+00:00",
                    llm_description=desc,
                    first_seen_ref=ref,
                )
            )
    body = portal.get("/api/projects/demo/scan-estimate").json()
    assert body["commits"] == 3 and body["large_commits"] == 1
    assert body["upper_bound"] is True
    assert body["tokens"]["input"] == (15 + 20 + 10) * CHARS_PER_LINE // CHARS_PER_TOKEN
    assert body["model"]["provider"] == "anthropic"
    assert body["missing_key"] == "anthropic"

    portal.put(
        "/api/projects/demo/config",
        json={"secrets": {"llm": {"anthropic": "sk-ant-estimate-1234"}}},
    )
    assert portal.get("/api/projects/demo/scan-estimate").json()["missing_key"] is None


# ---------------------------------------------------------------------------
# Lifespan exit with an open events stream (real uvicorn)
# ---------------------------------------------------------------------------


def test_shutdown_with_open_stream_interrupts_the_run(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    port = _free_port()
    app = create_portal_app(port=port)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            lifespan="on",
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started or not thread.is_alive())
    base = f"http://127.0.0.1:{port}"
    http = httpx.Client(
        base_url=base, headers=CLIENT_HEADER, trust_env=False, timeout=30
    )
    try:
        assert (
            http.post("/api/portal/setup", json={"display_name": "Tess"}).status_code
            == 201
        )
        root = make_repo(env.shared, "demo")
        seed_codegraph(root)
        assert (
            http.post(
                "/api/projects", json={"source": "local", "path": str(root)}
            ).status_code
            == 201
        )
        assert http.post("/api/projects/demo/init", json={"agents": []}).json()[
            "initialized"
        ]
        scanner.hold.touch()  # never released: the child runs until shutdown
        run_id = http.post("/api/projects/demo/scans").json()["run_id"]
        wait_for(lambda: len(scanner.calls()) == 1)

        received: list[str] = []
        ended = threading.Event()

        def follow() -> None:
            with httpx.Client(
                base_url=base, headers=CLIENT_HEADER, trust_env=False, timeout=30
            ) as c:
                try:
                    with c.stream(
                        "GET", f"/api/projects/demo/scans/{run_id}/events"
                    ) as r:
                        for chunk in r.iter_text():
                            received.append(chunk)
                except httpx.HTTPError:
                    pass
            ended.set()

        reader = threading.Thread(target=follow, daemon=True)
        reader.start()
        wait_for(lambda: any('"start"' in c for c in received))

        started = time.monotonic()
        server.should_exit = True
        thread.join(20)
        assert not thread.is_alive()
        assert time.monotonic() - started < 15
        assert ended.wait(5)
    finally:
        http.close()
        server.should_exit = True

    with portal_db.get_session() as session:
        run = session.get(ScanRun, run_id)
        assert run.status == "interrupted" and run.finished_at
    pid_alive = subprocess.run(
        ["pgrep", "-f", str(scanner.hold)], capture_output=True, text=True
    ).stdout.strip()
    assert pid_alive == ""  # the child (and its group) is gone


def test_open_stream_gets_the_shutdown_frame_without_holding_the_stop(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    """Fix pass 2: the production graceful timeout (10 s) is not sat out.

    uvicorn waits for open connections before the lifespan shutdown sets
    ``shutdown_event``; a plain ``uvicorn.Server`` therefore held the stop
    for the full timeout and cut the stream without ``event: shutdown``.
    """
    port = _free_port()
    app = create_portal_app(port=port)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_config=None,
        lifespan="on",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SEC,
    )
    server = PortalServer(config, app)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started or not thread.is_alive())
    base = f"http://127.0.0.1:{port}"
    http = httpx.Client(
        base_url=base, headers=CLIENT_HEADER, trust_env=False, timeout=30
    )
    try:
        http.post("/api/portal/setup", json={"display_name": "Tess"})
        root = make_repo(env.shared, "demo")
        seed_codegraph(root)
        http.post("/api/projects", json={"source": "local", "path": str(root)})
        http.post("/api/projects/demo/init", json={"agents": []})
        scanner.hold.touch()
        run_id = http.post("/api/projects/demo/scans").json()["run_id"]
        wait_for(lambda: len(scanner.calls()) == 1)

        received: list[str] = []

        def follow() -> None:
            with httpx.Client(
                base_url=base, headers=CLIENT_HEADER, trust_env=False, timeout=30
            ) as c:
                try:
                    with c.stream(
                        "GET", f"/api/projects/demo/scans/{run_id}/events"
                    ) as r:
                        for chunk in r.iter_text():
                            received.append(chunk)
                except httpx.HTTPError:
                    pass

        reader = threading.Thread(target=follow, daemon=True)
        reader.start()
        wait_for(lambda: any('"start"' in c for c in received))
        started = time.monotonic()
        server.should_exit = True
        thread.join(30)
        elapsed = time.monotonic() - started
        assert not thread.is_alive()
        assert elapsed < GRACEFUL_SHUTDOWN_SEC / 2, elapsed
        reader.join(5)
        assert any("event: shutdown" in c for c in received), received[-2:]
    finally:
        http.close()
        server.should_exit = True


def test_real_scan_child_with_hook_trigger(
    portal: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plan section 11.2 #3: the real `whygraph scan` as the child, so the argv
    # is the runner's own, on a repo that carries the portal markers.
    root = local_project(portal, env, "demo")
    assert (root / ".whygraph" / "portal.json").is_file()
    monkeypatch.setenv(
        "WHYGRAPH_SCAN_CMD",
        f"{shlex.quote(sys.executable)} -m whygraph scan --no-codegraph",
    )
    with portal_db.get_session() as session:  # pretend a first scan happened
        project = session.exec(select(Project).where(Project.slug == "demo")).one()
        project.last_scan_at = "2026-01-01T00:00:00+00:00"
        session.add(project)

    run = wait_run(portal, "demo", scan(portal, "demo", trigger="hook"), timeout=120)
    assert (run["status"], run["trigger"]) == ("ok", "hook"), run
    log = (env.data / "runs" / f"{run['id']}.log").read_text()
    assert "--progress json --managed-by-portal --skip-analyze --no-remote" in log
    events = [
        json.loads(line)
        for line in (env.data / "runs" / f"{run['id']}.jsonl").read_text().splitlines()
    ]
    assert events[0]["type"] == "start" and events[-1]["type"] == "result"
    assert events[-1]["status"] == "ok"
    assert run["summary"]["analyze_skipped"] == "--skip-analyze"
    assert portal.get("/api/projects/demo").json()["stats"]["commits"] == 2


# ---------------------------------------------------------------------------
# Security fix pass 1: symlinked DB paths are refused before a job runs
# ---------------------------------------------------------------------------


def _queue_behind_a_held_scan(
    client: TestClient, scanner: SimpleNamespace, slug: str
) -> int:
    """Start a scan that holds, and return its id once the child is running."""
    before = len(scanner.calls())
    scanner.hold.touch()
    running = scan(client, slug, trigger="manual")
    wait_for(lambda: len(scanner.calls()) == before + 1)
    return running


def _wait_row(run_id: int) -> ScanRun:
    def finished() -> ScanRun | None:
        with portal_db.get_session() as session:
            run = session.get(ScanRun, run_id)
            if run is not None and run.status in TERMINAL:
                session.expunge(run)
                return run
        return None

    return wait_for(finished)


def _swap_for_symlink(path: Path, target: Path) -> None:
    path.rename(target)
    path.symlink_to(target)


def test_runner_refuses_a_symlinked_db_instead_of_scanning(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    root = local_project(portal, env, "demo")
    first_scan(portal, "demo")
    running = _queue_behind_a_held_scan(portal, scanner, "demo")
    queued = scan(portal, "demo", trigger="manual")
    # The link lands after the request passed the HTTP gate.
    _swap_for_symlink(root / ".whygraph" / "whygraph.db", env.tmp / "elsewhere.db")
    scanner.hold.unlink()

    # The scans endpoints now answer 409 unsafe_path too; read the rows.
    assert _wait_row(running).status == "ok"
    run = _wait_row(queued)
    assert run.status == "failed"
    assert "symbolic link" in json.loads(run.summary)["error"]
    assert len(scanner.calls()) == 2  # no child for the refused run
    events = [
        json.loads(line)
        for line in (env.data / "runs" / f"{queued}.jsonl").read_text().splitlines()
    ]
    assert [e["type"] for e in events] == ["error"]
    log = (env.data / "runs" / f"{queued}.log").read_text()
    assert "refusing to scan" in log


# ---------------------------------------------------------------------------
# The run log tail (screen 7's log excerpt)
# ---------------------------------------------------------------------------


def test_scan_log_tail_is_bounded_redacted_and_scoped(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    local_project(portal, env, "other")
    llm_key = "sk-ant-SECRETkey000wxyz"
    portal.put(
        "/api/projects/demo/config",
        json={
            "config": {"llm": {"model": "anthropic/claude-opus-4-7"}},
            "secrets": {"llm": {"anthropic": llm_key}},
        },
    )
    first_scan(portal, "demo")
    scanner.configure(echo_env="ANTHROPIC_API_KEY")
    short = wait_run(portal, "demo", scan(portal, "demo", trigger="manual"))
    body = portal.get(f"/api/projects/demo/scans/{short['id']}/log").json()
    assert body["run_id"] == short["id"] and body["truncated"] is False
    assert body["size"] == len(body["text"].encode("utf-8"))
    assert llm_key not in body["text"] and f"value=…{llm_key[-4:]}" in body["text"]

    scanner.configure(stderr_bytes=300_000)
    big = wait_run(portal, "demo", scan(portal, "demo"))
    body = portal.get(f"/api/projects/demo/scans/{big['id']}/log").json()
    assert body["truncated"] is True and body["size"] > 300_000
    assert len(body["text"].encode("utf-8")) <= LOG_TAIL_BYTES
    full = (env.data / "runs" / f"{big['id']}.log").read_text()
    assert full.endswith(body["text"])
    assert full[: -len(body["text"])].endswith("\n")  # starts at a line boundary

    assert portal.get(f"/api/projects/other/scans/{big['id']}/log").status_code == 404
    assert portal.get("/api/projects/demo/scans/9999/log").status_code == 404


# ---------------------------------------------------------------------------
# Step 10b: the hook helper calls a live portal (plan section 11.3 item 5)
# ---------------------------------------------------------------------------


def test_commits_during_a_running_scan_yield_one_hook_follow_up(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    # The real helper, real git hooks and the real `curl` against a real
    # uvicorn portal: two commits while a scan runs coalesce into ONE queued
    # `trigger=hook` run; with the portal stopped a commit scans nothing and
    # leaves one line in hooks.log.
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_portal_app(port=port),
            host="127.0.0.1",
            port=port,
            log_config=None,
            lifespan="on",
            timeout_graceful_shutdown=1,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started or not thread.is_alive())
    http = httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers=CLIENT_HEADER,
        trust_env=False,
        timeout=30,
    )
    root = make_repo(env.shared, "demo")

    def commit(name: str) -> None:
        (root / name).write_text(name)
        _git(root, "add", name)
        _git(root, "commit", "-q", "-m", name)

    try:
        assert http.post("/api/portal/setup", json={"display_name": "T"}).is_success
        seed_codegraph(root)
        assert http.post(
            "/api/projects", json={"source": "local", "path": str(root)}
        ).is_success
        init = http.post("/api/projects/demo/init", json={"agents": []}).json()
        assert init["initialized"] and "post-commit" in init["hooks"]["installed"]
        with portal_db.get_session() as session:  # past the first (initial) scan
            project = session.exec(select(Project)).one()
            project.last_scan_at = "2026-01-01T00:00:00+00:00"
            session.add(project)

        scanner.hold.touch()
        running = http.post("/api/projects/demo/scans", json={"trigger": "manual"})
        running_id = running.json()["run_id"]
        wait_for(lambda: len(scanner.calls()) == 1)

        commit("one.txt")
        commit("two.txt")

        def pending() -> list[dict]:
            rows = http.get("/api/projects/demo/scans").json()["runs"]
            return [r for r in rows if r["id"] != running_id]

        queued = wait_for(pending)
        time.sleep(1.0)  # let the second hook's request land too
        (follow_up,) = pending()
        assert (follow_up["trigger"], follow_up["status"]) == ("hook", "queued")
        assert follow_up["requested_by"] is None
        assert queued[0]["id"] == follow_up["id"]

        scanner.hold.unlink()
        wait_for(lambda: len(scanner.calls()) == 2)
        assert "--skip-analyze" in runner_flags(scanner.calls()[1])
        assert "--no-remote" in runner_flags(scanner.calls()[1])
    finally:
        http.close()
        server.should_exit = True
        thread.join(20)

    log = root / ".whygraph" / "logs" / "hooks.log"
    assert not log.exists()  # every request above reached the portal
    before = len(scanner.calls())
    commit("three.txt")  # the portal is down
    wait_for(lambda: log.exists() and log.read_text().strip())
    time.sleep(0.5)
    assert len(log.read_text().splitlines()) == 1
    assert f"portal not reachable on port {port}" in log.read_text()
    assert len(scanner.calls()) == before
    assert not (root / ".whygraph" / "scan.lock").exists()


# ---------------------------------------------------------------------------
# Acceptance #18: a 1.x repo through the normal wizard (fix pass 2)
# ---------------------------------------------------------------------------

_V1_HOOK = (
    "#!/bin/sh\n"
    "# >>> whygraph managed >>>\n"
    'helper="$(git rev-parse --show-toplevel 2>/dev/null)/.whygraph/hooks/whygraph-scan"\n'
    '[ -x "$helper" ] && "$helper" "$@"\n'
    "# <<< whygraph managed <<<\n"
)
_V1_HELPER = "#!/bin/sh\nwhygraph scan --skip-analyze --no-remote --lock\n"
_STDIO = {"command": "whygraph-mcp"}


def test_a_1x_repo_migrates_through_the_wizard_and_keeps_its_commits(
    portal: TestClient, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from alembic import command

    from whygraph.db import bootstrap
    from whygraph.db import engine as db_engine
    from whygraph.hooks import LEGACY_HELPER_RELPATH, helper_path

    root = make_repo(env.shared, "legacy")
    seed_codegraph(root)
    first, second = _git(root, "rev-list", "--reverse", "HEAD").split()

    # A 1.x `.whygraph/whygraph.db` at an older revision, holding the first
    # commit (with a description a rescan must not throw away).
    old_revision = "4e231ec6f0e1"
    with use_project(manual_ctx(root)):
        (root / ".whygraph").mkdir()
        command.upgrade(bootstrap.alembic_config(), old_revision)
    db_engine._reset_engine()
    db = root / ".whygraph" / "whygraph.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            'INSERT INTO "commit" (sha, parent_shas, author_name, author_email,'
            " authored_at, committed_at, subject, body, files_changed, insertions,"
            " deletions, scanned_at, llm_description)"
            " VALUES (?, '[]', 'Test User', 'tester@example.com',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',"
            " 'first commit', '', 1, 2, 0, '2026-01-01T00:00:00+00:00', ?)",
            (first, "described by 1.x"),
        )
    # A managed 1.x hook + its work-tree helper, and two 1.x agent entries.
    hook = root / ".git" / "hooks" / "post-commit"
    hook.write_text(_V1_HOOK)
    hook.chmod(0o755)
    legacy = root / LEGACY_HELPER_RELPATH
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_V1_HELPER)
    legacy.chmod(0o755)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"whygraph": _STDIO}}))
    (root / ".vscode").mkdir()
    (root / ".vscode" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"whygraph": _STDIO}})
    )

    # Add: everything is detected, nothing is touched.
    detected = add_local(portal, root)["detected"]
    assert detected["existing_db"] is True
    assert detected["managed_hooks"] == ["post-commit"]
    agents = {d["agent"]: d for d in detected["detected_agents"]}
    assert set(agents) == {"claude", "vscode"}
    assert agents["vscode"]["stale"] is True
    assert hook.read_text() == _V1_HOOK  # the 1.x helper rescans until Initialize
    assert legacy.read_text() == _V1_HELPER

    # Initialize, migrating both agent files.
    done = init_project(
        portal,
        "legacy",
        agents=["claude", "vscode"],
        agent_actions={"claude": "migrate", "vscode": "migrate"},
    )
    assert done["initialized"] is True, done
    assert not (root / "whygraph.toml").exists()  # acceptance #4
    assert (root / ".whygraph" / "backups" / f"whygraph-{old_revision}.db").is_file()
    with sqlite3.connect(db) as conn:
        head = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert head != old_revision
    text = hook.read_text()
    assert "--git-common-dir" in text and ".whygraph/hooks" not in text
    assert not legacy.exists() and helper_path(root).is_file()
    url = "http://127.0.0.1:${WHYGRAPH_PORT:-8765}/mcp/legacy"
    claude = json.loads((root / ".mcp.json").read_text())
    assert claude["mcpServers"]["whygraph"] == {"type": "http", "url": url}
    vscode = json.loads((root / ".vscode" / "mcp.json").read_text())
    assert "whygraph" not in vscode.get("mcpServers", {})
    entry = vscode["servers"]["whygraph"]  # VS Code's own form (an input for the port)
    assert entry["type"] == "http" and entry["url"].endswith("/mcp/legacy")

    # The first scan (the real child) adds only the new commit.
    monkeypatch.setenv(
        "WHYGRAPH_SCAN_CMD",
        f"{shlex.quote(sys.executable)} -m whygraph scan --no-codegraph",
    )
    run = wait_run(portal, "legacy", scan(portal, "legacy"), timeout=120)
    assert (run["status"], run["trigger"]) == ("ok", "initial"), run
    with sqlite3.connect(db) as conn:
        rows = dict(
            conn.execute('SELECT sha, llm_description FROM "commit"').fetchall()
        )
    assert set(rows) == {first, second}
    assert rows[first] == "described by 1.x"


# ---------------------------------------------------------------------------
# Cancel (POST /scans/{id}/cancel)
# ---------------------------------------------------------------------------


def cancel(client: TestClient, slug: str, run_id: int):  # noqa: ANN201 -- a Response
    return client.post(f"/api/projects/{slug}/scans/{run_id}/cancel")


def test_cancel_a_running_scan(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    first_scan(portal, "demo")
    scanner.hold.touch()
    running = scan(portal, "demo", trigger="hook")
    wait_for(lambda: len(scanner.calls()) == 2)
    with portal_db.get_session() as session:
        before = session.exec(select(Project).where(Project.slug == "demo")).one()
        scanned_at = before.last_scan_at

    response = cancel(portal, "demo", running)

    assert response.status_code == 202, response.text
    assert response.json() == {"run_id": running, "was": "running"}
    run = wait_run(portal, "demo", running)
    assert run["status"] == "cancelled"  # never "interrupted"
    assert run["summary"]["cancelled_by"] == "user"
    with portal_db.get_session() as session:
        after = session.exec(select(Project).where(Project.slug == "demo")).one()
        assert after.last_scan_at == scanned_at  # a cancelled scan is not a scan
    # The project takes a fresh request right away.
    scanner.hold.unlink()
    assert (
        wait_run(portal, "demo", scan(portal, "demo", trigger="hook"))["status"] == "ok"
    )


def test_cancel_a_queued_scan_drops_it_from_the_queue(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    first_scan(portal, "demo")
    scanner.hold.touch()
    running = scan(portal, "demo", trigger="hook")
    wait_for(lambda: len(scanner.calls()) == 2)
    queued = scan(portal, "demo", trigger="manual")

    response = cancel(portal, "demo", queued)

    assert response.status_code == 200, response.text
    assert response.json() == {"run_id": queued, "was": "queued"}
    row = run_by_id(portal, "demo", queued)
    assert (row["status"], row["summary"]) == ("cancelled", {"cancelled_by": "user"})
    assert row["finished_at"]
    scanner.hold.unlink()
    assert wait_run(portal, "demo", running)["status"] == "ok"
    wait_idle(portal, "demo")
    assert len(scanner.calls()) == 2  # the cancelled run never started
    # A later request gets a new run, not the cancelled one.
    assert scan(portal, "demo", trigger="hook") not in (running, queued)


def test_cancel_escalates_to_sigkill(
    portal: TestClient,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "CANCEL_GRACE_SEC", 0.3)
    local_project(portal, env, "demo")
    first_scan(portal, "demo")
    scanner.configure(ignore_term=True)
    scanner.hold.touch()
    running = scan(portal, "demo", trigger="hook")
    wait_for(lambda: len(scanner.calls()) == 2)

    assert cancel(portal, "demo", running).status_code == 202
    assert wait_run(portal, "demo", running, timeout=10)["status"] == "cancelled"


def test_cancel_refuses_finished_unknown_and_foreign_runs(
    portal: TestClient, env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    local_project(portal, env, "demo")
    local_project(portal, env, "other")
    done = first_scan(portal, "demo")["id"]

    finished = cancel(portal, "demo", done)
    assert finished.status_code == 409
    assert "already ended (ok)" in finished.json()["error"]
    assert cancel(portal, "demo", 9999).status_code == 404
    assert cancel(portal, "other", done).status_code == 404  # another project's run
    assert run_by_id(portal, "demo", done)["status"] == "ok"


def test_cancel_after_the_runner_stopped_is_unavailable(
    env: SimpleNamespace, scanner: SimpleNamespace
) -> None:
    runner = ScanRunner()
    with client_for(runner) as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        local_project(client, env, "demo")
        done = first_scan(client, "demo")["id"]
    with pytest.raises(RunnerUnavailable):
        anyio.run(runner.cancel, 1, done)


def test_child_env_passes_the_claude_token_only_to_a_claude_cli_analyze_run(
    tmp_path: Path,
) -> None:
    config = Config.from_dict(
        {
            "llm": {
                "model": "claude-cli/claude-opus-4-7",
                "claude_cli": {"oauth_token": "sk-ant-oat-child1"},
            }
        },
        tmp_path,
    )
    env, secrets = child_env(config, {}, source="local", analyze=True, environ={})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-child1"
    assert secrets == ["sk-ant-oat-child1"]  # redacted from logs and events
    assert "sk-ant-oat-child1" not in env["WHYGRAPH_CONFIG_JSON"]

    structure_only, secrets = child_env(
        config, {}, source="local", analyze=False, environ={}
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in structure_only and secrets == []

    other = Config.from_dict(
        {
            "llm": {
                "model": "anthropic/claude-opus-4-7",
                "claude_cli": {"oauth_token": "sk-ant-oat-child1"},
            }
        },
        tmp_path,
    )
    env, _ = child_env(other, {}, source="local", analyze=True, environ={})
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_a_failing_outcome_write_is_logged_and_the_job_still_ends(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With the database gone at shutdown, recording a run must not hang or raise."""
    spec = runner_mod._Pending(
        run_id=7,
        project_id=1,
        kind="scan",
        trigger="manual",
        analyze=False,
        requested_by=None,
        scan_requested=False,
    )
    job = runner_mod._Job(spec=spec)
    runner = ScanRunner()
    monkeypatch.setattr(
        runner, "_execute_inner", lambda job: ("ok", {}, runner_mod.redactor([]))
    )

    def _gone(*_: object) -> None:
        raise RuntimeError("database connection lost")

    monkeypatch.setattr(runner_mod, "_finish_run", _gone)
    # A CLI test may have configured `whygraph` logging not to propagate.
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    with caplog.at_level("ERROR", logger=runner_mod.__name__):
        runner._execute(job)
    assert job.done.is_set()
    assert "could not record run 7" in caplog.text
