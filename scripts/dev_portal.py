"""Dev wrapper behind `make dev-local` and `make dev-docker`.

Runs the portal (``python -m whygraph portal <args>``) and Vite's dev server
together in the foreground, and restarts the portal whenever a backend source
file changes. Ctrl-C (or SIGTERM - what ``docker stop`` sends) stops both.

The restart happens *outside* the portal on purpose: the portal is exactly one
process (it holds ``<data>/portal.lock`` and keeps the scan runner in memory),
so a reload is a clean stop + start, never uvicorn's in-process reloader.
Stdlib only - it runs both from the uv venv and inside the Docker image.

Usage::

    python scripts/dev_portal.py [--no-reload] [--no-vite] [--vite-host H] \
        -- <whygraph portal args>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
PACKAGE = CHECKOUT / "src" / "whygraph"
PLAYGROUND = CHECKOUT / "src" / "playground"

WATCHED_SUFFIXES = frozenset({".py", ".md", ".toml"})
SKIPPED_DIRS = frozenset({"__pycache__", "static"})
POLL_SEC = 1.0
STOP_GRACE_SEC = 15.0  # above the portal's 10 s graceful shutdown
VITE_PORT = 5173
LOCK_STAMP = ".whygraph-lock-sha256"


def snapshot(root: Path) -> dict[Path, float]:
    """Map every watched file under ``root`` to its mtime."""
    found: dict[Path, float] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix not in WATCHED_SUFFIXES:
                continue
            try:
                found[path] = path.stat().st_mtime
            except OSError:  # deleted between walk and stat
                continue
    return found


def first_change(before: dict[Path, float], after: dict[Path, float]) -> Path | None:
    """The first added, removed or modified file between two snapshots."""
    for path, mtime in after.items():
        if before.get(path) != mtime:
            return path
    for path in before:
        if path not in after:
            return path
    return None


def portal_port(portal_args: list[str]) -> int:
    """The ``--port`` among the portal args, else ``$WHYGRAPH_PORT`` or 8765."""
    for i, arg in enumerate(portal_args):
        if arg == "--port" and i + 1 < len(portal_args):
            return int(portal_args[i + 1])
        if arg.startswith("--port="):
            return int(arg.split("=", 1)[1])
    raw = os.environ.get("WHYGRAPH_PORT", "")
    return int(raw) if raw.isdigit() else 8765


def _say(msg: str) -> None:
    print(f"[dev] {msg}", flush=True)


def _start(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.Popen:
    # Own process group, so a stop reaches the child's own children (Vite's
    # esbuild) and Ctrl-C reaches only this wrapper. Scan children run in
    # their own sessions: the portal's shutdown stops them, which is why the
    # grace below is longer than the portal's.
    return subprocess.Popen(cmd, env=env, start_new_session=True)


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=STOP_GRACE_SEC)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def _ensure_node_modules() -> None:
    """Install the playground's deps when missing or out of date.

    A stamp of ``package-lock.json``'s hash is kept inside ``node_modules``
    (which is a separate, Linux-only mount in the container), so each side
    installs once and again only after the lockfile changes.
    """
    lock = PLAYGROUND / "package-lock.json"
    modules = PLAYGROUND / "node_modules"
    stamp = modules / LOCK_STAMP
    want = hashlib.sha256(lock.read_bytes()).hexdigest()
    if stamp.is_file() and stamp.read_text().strip() == want:
        return
    _say("installing playground dependencies (npm ci)...")
    subprocess.run(["npm", "--prefix", str(PLAYGROUND), "ci"], check=True)
    stamp.write_text(want + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--no-reload", action="store_true", help="never restart the portal"
    )
    parser.add_argument("--no-vite", action="store_true", help="run the portal only")
    parser.add_argument("--vite-host", default="127.0.0.1", help="Vite bind address")
    parser.add_argument("portal_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    portal_args = args.portal_args
    if portal_args[:1] == ["--"]:  # argparse keeps the separator on some versions
        portal_args = portal_args[1:]

    stopping = False

    def _on_signal(_sig: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    port = portal_port(portal_args)
    portal_cmd = [sys.executable, "-m", "whygraph", "portal", *portal_args]
    vite: subprocess.Popen | None = None
    if not args.no_vite:
        _ensure_node_modules()
        vite_env = {**os.environ, "WHYGRAPH_DEV_PORTAL": f"http://127.0.0.1:{port}"}
        vite = _start(
            [
                "npm", "--prefix", str(PLAYGROUND), "run", "dev", "--",
                "--host", args.vite_host, "--port", str(VITE_PORT), "--strictPort",
            ],
            env=vite_env,
        )  # fmt: skip
        _say(f"playground (HMR) -> http://localhost:{VITE_PORT}   (open this)")
    _say(f"portal + MCP      -> http://127.0.0.1:{port}   (/mcp/<slug>)")
    if not args.no_reload:
        _say(f"restarting the portal on changes under {PACKAGE.relative_to(CHECKOUT)}/")

    portal = _start(portal_cmd)
    seen = snapshot(PACKAGE) if not args.no_reload else {}
    reported_exit = False
    code = 0
    try:
        while not stopping:
            time.sleep(POLL_SEC)
            if vite is not None and vite.poll() is not None:
                _say(f"Vite exited (code {vite.returncode}) - stopping")
                code = vite.returncode or 1
                break
            if portal.poll() is not None and not reported_exit:
                reported_exit = True
                if args.no_reload:
                    code = portal.returncode
                    break
                _say(
                    f"portal exited (code {portal.returncode}) - "
                    "fix the error and save; it restarts on the next change"
                )
            if args.no_reload:
                continue
            now = snapshot(PACKAGE)
            changed = first_change(seen, now)
            if changed is None:
                continue
            seen = now
            _say(f"↻ backend changed ({changed.relative_to(CHECKOUT)}) - restarting")
            _stop(portal)
            portal = _start(portal_cmd)
            reported_exit = False
    finally:
        _say("stopping")
        _stop(portal)
        _stop(vite)
    return code


if __name__ == "__main__":
    sys.exit(main())
