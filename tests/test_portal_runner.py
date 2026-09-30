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
from sqlmodel import select

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
from whygraph.core.config import Config
from whygraph.core.context import use_project
from whygraph.db import get_session as project_session
from whygraph.db.models import Commit
from whygraph.portal import db as portal_db
from whygraph.portal.app import create_portal_app
from whygraph.portal.estimate import (
    CHARS_PER_LINE,
    CHARS_PER_TOKEN,
    OUTPUT_TOKENS_PER_COMMIT,
    SYNTHESIS_INPUT_TOKENS,
    CommitSize,
    estimate_tokens,
    render_estimate,
)
from whygraph.portal.models import Project, ScanRun
from whygraph.portal.runner import (
    LOG_TAIL_BYTES,
    TRIGGER_PRECEDENCE,
    ScanRunner,
    child_env,
    merge_trigger,
    redactor,
    resolve_analyze,
    scan_argv,
    scan_flags,
)
from whygraph.services.git import Repository

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
            parts += [f"--{key.replace('_', '-')}", str(value)]
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


def github_project(
    client: TestClient, env: SimpleNamespace, name: str
) -> tuple[Path, Path]:
    """A "GitHub clone" under ``<data>/repos`` whose origin is a local upstream."""
    upstream = make_repo(env.tmp / "upstream", name)
    dest = env.data / "repos" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(dest)],
        check=True,
        capture_output=True,
    )
    seed_codegraph(dest)
    with portal_db.get_session() as session:
        session.add(
            Project(
                slug=name,
                name=name,
                source="github",
                root=f"repos/{name}",
                remote_url=f"https://github.com/acme/{name}",
            )
        )
    assert init_project(client, name)["initialized"] is True
    return upstream, dest


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


@pytest.fixture
def plain_fetch(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace the https-only GitHub fetch with a plain ``git fetch`` of a local origin."""
    gate = SimpleNamespace(hold=None, calls=0)

    def fetch_default(self: Repository, *, env=None, timeout=900) -> None:
        gate.calls += 1
        while gate.hold is not None and gate.hold.exists():
            time.sleep(0.02)
        subprocess.run(
            ["git", "fetch", "-q", "origin"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    monkeypatch.setattr(Repository, "fetch_default", fetch_default)
    return gate


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

    structure_only, secrets = child_env(
        config, layer, source="github", analyze=False, environ=portal_env
    )
    assert "ANTHROPIC_API_KEY" not in structure_only
    assert structure_only["WHYGRAPH_GIT_TOKEN"] == "ghp_project"
    assert secrets == ["ghp_project"]

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


def test_sync_holds_the_slot_while_a_scan_waits(
    portal: TestClient,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    plain_fetch: SimpleNamespace,
) -> None:
    upstream, clone = github_project(portal, env, "gh")
    first_scan(portal, "gh")
    new_head = commit(upstream, "moved upstream\n")

    plain_fetch.hold = env.tmp / "fetch-hold"
    plain_fetch.hold.touch()
    sync_id = portal.post("/api/projects/gh/sync").json()["run_id"]
    wait_for(lambda: plain_fetch.calls == 1)
    scan_id = scan(portal, "gh", trigger="manual")
    assert scan_id != sync_id
    time.sleep(0.3)
    assert run_by_id(portal, "gh", sync_id)["status"] == "running"
    assert run_by_id(portal, "gh", scan_id)["status"] == "queued"
    assert len(scanner.calls()) == 1  # only the first scan so far

    plain_fetch.hold.unlink()
    sync = wait_run(portal, "gh", sync_id)
    assert (sync["kind"], sync["trigger"], sync["status"]) == ("sync", "sync", "ok")
    assert sync["summary"]["moved"] is True
    assert wait_run(portal, "gh", scan_id)["status"] == "ok"
    assert _git(clone, "rev-parse", "HEAD").strip() == new_head
    calls = scanner.calls()
    assert len(calls) == 3  # the sync's own follow-up scan, then the manual one
    assert runner_flags(calls[1])[3:] == ["--skip-analyze"]
    assert runner_flags(calls[2])[3:] == []

    # A sync that does not move HEAD does not scan.
    run = wait_run(portal, "gh", portal.post("/api/projects/gh/sync").json()["run_id"])
    assert run["status"] == "ok" and run["summary"]["moved"] is False
    assert len(scanner.calls()) == 3


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


def test_poll_tick_syncs_github_clones_only_and_catches_up(
    env: SimpleNamespace, scanner: SimpleNamespace, plain_fetch: SimpleNamespace
) -> None:
    clock = FakeClock()
    with client_for(ScanRunner(sleep=clock.sleep)) as client:
        client.post("/api/portal/setup", json={"display_name": "Tess"})
        local = local_project(client, env, "loc")
        github_project(client, env, "gh")
        first_scan(client, "loc")
        first_scan(client, "gh")
        assert clock.requested == [900]

        clock.advance()  # tick 1: HEADs match, so only the GitHub poll
        wait_for(lambda: len(runs(client, "gh")) == 2)
        wait_idle(client, "gh")
        poll = runs(client, "gh")[0]
        assert (poll["kind"], poll["trigger"], poll["requested_by"]) == (
            "sync",
            "poll",
            None,
        )
        assert poll["summary"]["moved"] is False
        assert len(runs(client, "loc")) == 1

        commit(local, "a commit the hook never reported\n")
        wait_for(lambda: len(clock.requested) == 2)
        clock.advance()  # tick 2: the local HEAD moved -> one catch-up hook scan
        wait_for(lambda: len(runs(client, "loc")) == 2)
        catch_up = wait_idle(client, "loc")[0]
        assert (catch_up["trigger"], catch_up["status"]) == ("hook", "ok")
        assert runner_flags(scanner.calls()[-1])[3:] == [
            "--skip-analyze",
            "--no-remote",
        ]
        assert len(runs(client, "gh")) == 3  # the second poll
        assert client.get("/api/projects").json()["projects"][1]["stale"] is None


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


def test_runner_refuses_a_symlinked_db_instead_of_syncing(
    portal: TestClient,
    env: SimpleNamespace,
    scanner: SimpleNamespace,
    plain_fetch: SimpleNamespace,
) -> None:
    _, clone = github_project(portal, env, "gh")
    first_scan(portal, "gh")
    running = _queue_behind_a_held_scan(portal, scanner, "gh")
    sync_id = portal.post("/api/projects/gh/sync").json()["run_id"]
    assert sync_id != running
    _swap_for_symlink(clone / ".codegraph" / "codegraph.db", env.tmp / "cg.db")
    scanner.hold.unlink()

    run = _wait_row(sync_id)
    assert (run.kind, run.status) == ("sync", "failed")
    assert "symbolic link" in json.loads(run.summary)["error"]
    assert portal.get("/api/projects/gh/scans").json()["code"] == "unsafe_path"
    assert plain_fetch.calls == 0
    assert len(scanner.calls()) == 2


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
