"""Tests for the CodeGraph ops surface (portal-hosted CodeGraph plan, step 3c, section 4.2c).

Reuses the fake ``codegraph`` and the helpers of
``test_portal_codegraph_host.py``.
"""

# ruff: noqa: F811 -- pytest fixtures are imported and re-declared as arguments
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from test_portal_app import (  # noqa: F401 -- `env` is a fixture
    env,
    make_repo,
    portal_client,
    seed_codegraph,
)
from test_portal_codegraph_host import (  # noqa: F401 -- `fake_cg` is a fixture
    _alive,
    _call,
    _host,
    _indexed_repo,
    _search,
    _target,
    _text,
    fake_cg,
)
from test_portal_mcp import _setup_project
from whygraph.core.config import Config, ConfigError
from whygraph.portal import db as portal_db
from whygraph.portal.codegraph_host import (
    PAUSED,
    TURNED_OFF,
    BridgeTarget,
    HostBusy,
)
from whygraph.portal.models import ScanRun
from whygraph.portal.policy import IMPORT_ALLOWLIST, LINKED_PUT_ALLOWLIST
from whygraph.services.codegraph.fingerprint import fingerprint

SNAPSHOT_KEYS = {
    "server",
    "last_call_at",
    "last_sync",
    "sync_failures_10m",
    "sync_paused_until",
    "guard",
    "withheld_tools",
    "since_start",
}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _wait_live(host: object) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and host.state != "live":  # type: ignore[attr-defined]
        time.sleep(0.05)
    assert host.state == "live"  # type: ignore[attr-defined]


def _tool_error(result: object) -> str:
    return result.content[0].text if result.isError else "ok"  # type: ignore[attr-defined]


# -- the snapshot -----------------------------------------------------------


def test_snapshot_across_start_crash_window_sync_failure_and_guard(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    root = _indexed_repo(tmp_path)
    clock = _Clock()
    out: dict[str, object] = {}

    async def main() -> None:
        host = _host(fake_cg, tmp_path / "data", clock=clock)
        await host.start()
        try:
            await host.session()
            snap = host.snapshot(1)
            out["live"] = snap
            assert set(snap) == SNAPSHOT_KEYS
            # A changed tree: the call refreshes, then forwards.
            assert _tool_error(await _search(host, root)) == "ok"
            out["synced"] = host.snapshot(1)
            # A failing refresh.
            fake_cg.configure(sync_exit=1)
            (root / "a.py").write_text("def f():\n    return 2\n")
            assert "could not be refreshed" in _tool_error(await _search(host, root))
            out["failed"] = host.snapshot(1)
            # A crash, then a second one inside the window.
            fake_cg.configure(exit_on="codegraph_node")
            host._child.stop.set()
            await host._child.done.wait()
            await host.session()
            args = {"symbol": "s"}
            host.note_scan_index(1, fingerprint(root))
            await host.call_tool(_target(root), "codegraph_node", args)  # crash
            await host.call_tool(_target(root), "codegraph_search", {"query": "q"})
            await host.call_tool(_target(root), "codegraph_node", args)  # again
            clock.now += 10
            assert PAUSED[:20] in _tool_error(await _search(host, root))
            out["window"] = host.snapshot(1)
            # A guard refusal.
            (root / "link").symlink_to("/etc")
            host.note_scan_index(1, None)
            await _search(host, root)
            out["guard"] = host.snapshot(1)
        finally:
            await host.shutdown()

    anyio.run(main)
    live = out["live"]
    assert live["server"]["state"] == "live" and live["server"]["shared"] is True
    assert live["server"]["available"] is True and live["server"]["pid"]
    assert live["server"]["since"] and live["last_sync"] is None
    synced = out["synced"]
    assert synced["last_sync"]["result"] == "ok"
    assert synced["since_start"]["calls"] == 1 and synced["last_call_at"]
    assert synced["guard"]["ok"] is True and synced["guard"]["paths"] == []
    failed = out["failed"]
    assert failed["last_sync"]["result"] == "failed"
    assert (
        failed["sync_failures_10m"] == 1
        and failed["since_start"]["refresh_failed"] == 1
    )
    assert out["window"]["server"]["state"] == "restart_window"
    guard = out["guard"]["guard"]
    assert guard["ok"] is False and guard["paths"] == ["link"]


def test_snapshot_pause_is_reported_with_a_deadline(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(sync_exit=1)
    root = _indexed_repo(tmp_path)

    async def main() -> dict:
        host = _host(fake_cg, tmp_path / "data")
        await host.start()
        try:
            for _ in range(3):
                await _search(host, root)
            return host.snapshot(1)
        finally:
            await host.shutdown()

    snap = anyio.run(main)
    assert snap["sync_paused_until"]


def test_a_failed_start_is_unavailable_and_no_binary_is_off(
    fake_cg: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_cg.configure(protocol="1999-01-01")

    async def failed() -> str:
        host = _host(fake_cg, tmp_path / "data")
        await host.start()
        try:
            with anyio.fail_after(10):
                await host._child.done.wait()
            return host.snapshot(1)["server"]["state"]
        finally:
            await host.shutdown()

    assert anyio.run(failed) == "unavailable"

    async def absent() -> dict:
        host = CodeGraphHostFor(tmp_path / "data2", tmp_path / "nope")
        await host.start()
        try:
            return host.snapshot(1)
        finally:
            await host.shutdown()

    snap = anyio.run(absent)
    assert snap["server"]["state"] == "off" and snap["server"]["available"] is False
    assert snap["server"]["pid"] is None and snap["since_start"]["calls"] == 0


def CodeGraphHostFor(data: Path, binary: Path):  # noqa: N802
    from whygraph.portal.codegraph_host import CodeGraphHost

    return CodeGraphHost(data, argv=[str(binary), "serve", "--mcp"])


def test_tallies_count_busy_refused_and_timeouts(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(sleep=1.0)
    root = _indexed_repo(tmp_path)

    async def main() -> dict:
        host = _host(fake_cg, tmp_path / "data", call_timeout=0.3)
        await host.start()
        try:
            host.note_scan_index(1, fingerprint(root))
            await host.call_tool(_target(root), "codegraph_node", {"file": "/etc/x"})
            await _search(host, root)  # times out
            return host.snapshot(1)["since_start"]
        finally:
            await host.shutdown()

    tally = anyio.run(main)
    assert tally["calls"] == 2 and tally["refused"] == 1 and tally["timeouts"] == 1


# -- the off switch ---------------------------------------------------------


def test_serve_codegraph_false_answers_turned_off_before_anything_else(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    root = _indexed_repo(tmp_path)
    off = BridgeTarget(1, "demo", root, os.path.realpath(root), serve=False)

    async def main() -> str:
        host = _host(fake_cg, tmp_path / "data")
        await host.start()
        try:
            return _tool_error(
                await host.call_tool(off, "codegraph_search", {"query": "q"})
            )
        finally:
            await host.shutdown()

    assert anyio.run(main) == TURNED_OFF
    assert fake_cg.call_list() == []


def test_serve_codegraph_config_and_allowlists(tmp_path: Path) -> None:
    assert Config.from_dict({}, tmp_path).scan_serve_codegraph is True
    cfg = Config.from_dict({"scan": {"serve_codegraph": False}}, tmp_path)
    assert cfg.scan_serve_codegraph is False
    with pytest.raises(ConfigError):
        Config.from_dict({"scan": {"serve_codegraph": "no"}}, tmp_path)
    assert IMPORT_ALLOWLIST["scan"]["serve_codegraph"] is True
    assert LINKED_PUT_ALLOWLIST["scan"]["serve_codegraph"] is True


def test_the_off_switch_reaches_the_tools_through_the_config_route(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    with portal_client() as client:
        _setup_project(client, root)
        assert (
            _call(client, "codegraph_search", {"query": "q"})["result"]["isError"]
            is False
        )
        put = client.put(
            "/api/projects/demo/config",
            json={"config": {"scan": {"serve_codegraph": False}}},
        )
        assert put.status_code == 200, put.text
        message = _call(client, "codegraph_search", {"query": "q"})
        assert message["result"]["isError"] is True and _text(message) == TURNED_OFF
        overview = client.get("/api/projects/demo/overview").json()["codegraph"]
        assert overview["enabled"] is False


# -- the overview block, routes, portal state --------------------------------


def test_overview_codegraph_block_index_and_last_index_failure(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    with portal_client() as client:
        _setup_project(client, root)
        _call(client, "codegraph_search", {"query": "q"})
        block = client.get("/api/projects/demo/overview").json()["codegraph"]
        assert set(block) == SNAPSHOT_KEYS | {"enabled", "index", "last_index_failure"}
        assert block["enabled"] is True and block["server"]["state"] == "live"
        assert block["index"] == {"present": True, "files": 0}
        assert block["last_index_failure"] is None
        assert block["since_start"]["calls"] == 1

        def run(status: str, codegraph: dict | None, minute: int) -> int:
            stamp = f"2026-01-01T00:{minute:02d}:00+00:00"
            with portal_db.get_session() as session:
                row = ScanRun(
                    project_id=1,
                    kind="scan",
                    trigger="manual",
                    analyze=False,
                    status=status,
                    queued_at=stamp,
                    started_at=stamp,
                    finished_at=stamp,
                    summary=json.dumps({"codegraph": codegraph} if codegraph else {}),
                )
                session.add(row)
                session.flush()
                return row.id

        run("ok", {"status": "failed", "error": "boom", "files": 0}, 1)
        failed = run("ok", {"status": "failed", "error": "later boom", "files": 0}, 2)
        failure = client.get("/api/projects/demo/overview").json()["codegraph"][
            "last_index_failure"
        ]
        assert failure["run_id"] == failed and failure["message"] == "later boom"
        run("ok", {"status": "ok", "files": 3}, 3)
        after = client.get("/api/projects/demo/overview").json()["codegraph"]
        assert after["last_index_failure"] is None


def test_overview_index_is_absent_without_a_database(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    root = make_repo(env.shared, "demo")
    with portal_client() as client:
        _setup_project(client, root)
        block = client.get("/api/projects/demo/overview").json()["codegraph"]
    assert block["index"] == {"present": False, "files": None}


def test_log_route_tails_redacted_and_404s_without_a_file(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    token = "ghp_" + "d" * 36
    with portal_client() as client:
        _setup_project(client, root)
        log = env.data / "logs" / "codegraph.log"
        log.unlink(missing_ok=True)
        assert client.get("/api/projects/demo/codegraph/log").status_code == 404
        log.write_text("x" * 70_000 + f"\nlast line {token}\n")
        body = client.get("/api/projects/demo/codegraph/log").json()
    assert body["truncated"] is True and body["size"] > 70_000
    assert "last line ghp_***" in body["text"] and token not in body["text"]
    assert len(body["text"]) <= 64 * 1024


def test_restart_route_restarts_the_shared_child_and_clears_the_window(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    root = make_repo(env.shared, "demo")
    seed_codegraph(root)
    with portal_client() as client:
        _setup_project(client, root)
        host = client.app.state.portal.codegraph_host
        _wait_live(host)
        before = host.pid
        assert before
        response = client.post("/api/projects/demo/codegraph/restart")
        assert response.status_code == 200, response.text
        assert response.json() == {"was": "live", "shared": True}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and host.pid in (None, before):
            time.sleep(0.05)
        assert host.pid not in (None, before)
        assert not _alive(before)
        # A crash window is cleared.
        host._crash_restart_at = host.clock()
        host._child.stop.set()
        time.sleep(0.5)
        assert client.post("/api/projects/demo/codegraph/restart").status_code == 200
        assert host._window_remaining() <= 0
        host._restarting = True
        busy = client.post("/api/projects/demo/codegraph/restart")
        host._restarting = False
        assert busy.status_code == 409 and busy.json()["code"] == "busy"


def test_restart_raises_host_busy_while_a_restart_runs(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    async def main() -> None:
        host = _host(fake_cg, tmp_path / "data")
        await host.start()
        try:
            host._restarting = True
            with pytest.raises(HostBusy):
                await host.restart(1)
        finally:
            host._restarting = False
            await host.shutdown()

    anyio.run(main)


def test_restart_clears_the_projects_sync_pause(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    fake_cg.configure(sync_exit=1)
    root = _indexed_repo(tmp_path)

    async def main() -> tuple[dict, dict, str]:
        host = _host(fake_cg, tmp_path / "data")
        await host.start()
        try:
            for _ in range(3):
                await _search(host, root)
            paused = host.snapshot(1)
            was = await host.restart(1)
            return paused, host.snapshot(1), was
        finally:
            await host.shutdown()

    paused, cleared, was = anyio.run(main)
    assert paused["sync_paused_until"] and cleared["sync_paused_until"] is None
    assert was == "live"


def test_portal_state_names_codegraph_counts_only(
    env: SimpleNamespace,  # noqa: F811
    fake_cg: SimpleNamespace,
) -> None:
    with portal_client() as client:
        _wait_live(client.app.state.portal.codegraph_host)
        state = client.get("/api/portal/state").json()
    assert state["codegraph"] == {"available": True, "live": 1}


def test_portal_state_without_a_binary(
    env: SimpleNamespace,  # noqa: F811
) -> None:
    with portal_client() as client:
        state = client.get("/api/portal/state").json()
    assert state["codegraph"] == {"available": False, "live": 0}


# -- the orphan sweep --------------------------------------------------------


def test_the_orphan_sweep_kills_a_recorded_leftover_and_ignores_a_stranger(
    fake_cg: SimpleNamespace, tmp_path: Path
) -> None:
    data = tmp_path / "data"
    hosts = data / "locks" / "codegraph" / "hosts.json"
    hosts.parent.mkdir(parents=True)

    leftover = subprocess.Popen(
        [str(fake_cg.bin), "serve", "--mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        time.sleep(0.5)

        async def sweep(pid: int) -> None:
            hosts.write_text(json.dumps({"pid": pid, "started": "x"}))
            host = _host(fake_cg, data)
            await host.start()
            await host.shutdown()

        anyio.run(sweep, stranger.pid)
        assert stranger.poll() is None  # not ours: left alone
        anyio.run(sweep, leftover.pid)
        deadline = time.monotonic() + 10
        while leftover.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert leftover.poll() is not None
        assert json.loads(hosts.read_text())["pid"] is None
    finally:
        for proc in (leftover, stranger):
            if proc.poll() is None:
                proc.kill()
            proc.wait()
