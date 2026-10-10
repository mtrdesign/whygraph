"""A stand-in for ``whygraph scan --progress json``, for the scan runner tests.

Used through ``WHYGRAPH_SCAN_CMD="<python> tests/fixtures/fake_scan.py <options>"``;
the runner appends ``--progress json --managed-by-portal <flags>``, which
land in the recorded argv. Options (all before the runner's own flags):

``--record FILE``        append ``{"argv", "env", "cwd"}`` as one JSON line
``--hold FILE``          after the first event, wait while FILE exists
``--exit N``             exit code (default 0)
``--stderr-bytes N``     write N bytes of noise to stderr before exiting
``--echo-env VAR``       print VAR's value in a stdout event and on stderr
``--codegraph-only``     accepted (the runner passes it for a linked project); like
                         every run it writes nothing but the events: no whygraph.db
``--ignore-term``        ignore SIGTERM (only SIGKILL stops it)
``--usage JSON``         a JSON list of objects, each emitted as a ``usage`` event
                         (``{"type": "usage", **obj}``) after the first phase and
                         before any hold
``--result-extra JSON``  a JSON object merged into the ``result`` event
``--token-check URL``    read the GitHub token (``github_token()``: the portal's
                         token file, else ``GH_TOKEN``), GET URL with it as git's
                         Basic password, wait ``--token-wait SEC`` (default 0),
                         read and check again; exit 3 / 4 when the first / second
                         check is refused. Each token goes to stderr and, with
                         ``--token-record FILE``, one per line to FILE
"""

from __future__ import annotations

import base64
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request


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


def _token_ok(url: str, token: str | None) -> bool:
    if not token:
        return False
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {basic}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status == 200
    except urllib.error.URLError:
        return False


def _check_tokens(url: str, wait: float, record: str | None) -> int:
    from whygraph.services.github.token import github_token

    for attempt, code in ((0, 3), (1, 4)):
        if attempt:
            time.sleep(wait)
        token = github_token()
        sys.stderr.write(f"token value={token}\n")
        sys.stderr.flush()
        if record:
            with open(record, "a", encoding="utf-8") as fh:
                fh.write(f"{token}\n")
        if not _token_ok(url, token):
            return code
    return 0


def main() -> int:
    args = sys.argv[1:]
    record = _take(args, "--record")
    hold = _take(args, "--hold")
    code = int(_take(args, "--exit") or 0)
    noise = int(_take(args, "--stderr-bytes") or 0)
    echo = _take(args, "--echo-env")
    token_check = _take(args, "--token-check")
    token_wait = float(_take(args, "--token-wait") or 0)
    token_record = _take(args, "--token-record")
    usage = json.loads(_take(args, "--usage") or "[]")
    result_extra = json.loads(_take(args, "--result-extra") or "{}")
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
    _emit(
        {
            "type": "start",
            "phase_total": 2,
            "phases": ["Structural crawl", "Author identity"],
        }
    )
    _emit({"type": "phase", "phase": 1, "title": "Structural crawl"})
    if echo:
        value = os.environ.get(echo, "")
        _emit({"type": "task", "name": "echo", "description": f"value={value}"})
        sys.stderr.write(f"stderr value={value}\n")
        sys.stdout.write(f"plain stdout value={value}\n")
        sys.stdout.flush()
    for event in usage:
        _emit({"type": "usage", **event})
    while hold and os.path.exists(hold):
        time.sleep(0.02)
    if token_check:
        checked = _check_tokens(token_check, token_wait, token_record)
        if checked:
            return checked
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
            **result_extra,
        }
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
