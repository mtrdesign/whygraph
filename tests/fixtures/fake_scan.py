"""A stand-in for ``whygraph scan --progress json``, for the scan runner tests.

Used through ``WHYGRAPH_SCAN_CMD="<python> tests/fixtures/fake_scan.py <options>"``;
the runner appends ``--progress json --managed-by-portal <flags>``, which
land in the recorded argv. Options (all before the runner's own flags):

``--record FILE``        append ``{"argv", "env", "cwd"}`` as one JSON line
``--hold FILE``          after the first event, wait while FILE exists
``--exit N``             exit code (default 0)
``--stderr-bytes N``     write N bytes of noise to stderr before exiting
``--echo-env VAR``       print VAR's value in a stdout event and on stderr
``--ignore-term``        ignore SIGTERM (only SIGKILL stops it)
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time


def _take(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        value = args[i + 1]
        del args[i : i + 2]
        return value
    return None


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    args = sys.argv[1:]
    record = _take(args, "--record")
    hold = _take(args, "--hold")
    code = int(_take(args, "--exit") or 0)
    noise = int(_take(args, "--stderr-bytes") or 0)
    echo = _take(args, "--echo-env")
    if "--ignore-term" in args:
        args.remove("--ignore-term")
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if record:
        with open(record, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}
                )
                + "\n"
            )
    _emit({"type": "start", "phase_total": 2})
    _emit({"type": "phase", "phase": 1, "title": "Structural crawl"})
    if echo:
        value = os.environ.get(echo, "")
        _emit({"type": "task", "name": "echo", "description": f"value={value}"})
        sys.stderr.write(f"stderr value={value}\n")
        sys.stdout.write(f"plain stdout value={value}\n")
        sys.stdout.flush()
    while hold and os.path.exists(hold):
        time.sleep(0.02)
    if noise:
        chunk = "x" * 1023 + "\n"
        for _ in range(noise // 1024 + 1):
            sys.stderr.write(chunk)
        sys.stderr.flush()
    _emit({"type": "phase", "phase": 2, "title": "Author identity"})
    _emit(
        {
            "type": "result",
            "status": "ok" if code == 0 else "failed",
            "elapsed_sec": 0.01,
            "phase_timings": {},
            "crawlers": [],
            "analyze_skipped": None,
        }
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
