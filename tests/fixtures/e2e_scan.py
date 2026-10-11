"""A slow stand-in for ``whygraph scan --progress json``, for the Playwright suite.

Used through ``WHYGRAPH_SCAN_CMD="python3 tests/fixtures/e2e_scan.py --control DIR"``
(``make e2e`` sets it). Unlike ``fake_scan.py`` (instant, for runner tests) this
one takes a moment per phase so a browser can watch the progress bars move, and
it does what a real structural scan leaves behind: it writes a small CodeGraph
index into ``<cwd>/.codegraph/codegraph.db`` (the runner starts the child in the
project root). The symbols are named after the repo folder, so two projects
never share one - which is what the project-switch test relies on.

Options (before the runner's own ``--progress json --managed-by-portal ...``):

``--control DIR``   look for ``DIR/fail`` (the ``git`` crawler fails in phase
                    1, the later phases never start, exit 1, with a stderr
                    line carrying a fake key so redaction can be checked) and
                    ``DIR/delay`` (seconds per tick) on every run
``--delay SEC``     seconds per tick when ``DIR/delay`` is absent (default 0.15)

With ``--codegraph-only`` (a project linked to a platform) it runs one phase,
seeds the CodeGraph index and touches nothing else; ``DIR/fail`` fails it.

With ``--real-git`` (the production portal, so a platform has real evidence to
answer a linked project's questions with) it seeds the CodeGraph index and then
**hands over to the real scanner** for the git crawl, offline: ``python -m
whygraph scan`` with the runner's own arguments plus ``--skip-analyze``
``--no-remote`` ``--no-codegraph``, its progress stream inherited. That child
needs a real interpreter for the checkout (``E2E_SCAN_PYTHON``), unlike every
other path here. ``DIR/fail`` fails it without running anything.

A production portal's runner passes a GitHub project's installation token
as a file (``WHYGRAPH_GITHUB_TOKEN_FILE``); when that variable is set the
file must be readable and non-empty, else the scan fails at once, as a real
scan's ``git`` / ``gh`` calls would.

Standard library only (bar the ``--real-git`` child), so any ``python3`` will
run it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

OWN_FLAGS = ("--control", "--delay")
"""This fixture's own options that take a value (dropped when forwarding)."""

OWN_SWITCHES = ("--codegraph-only", "--real-git")
"""This fixture's own valueless options (dropped when forwarding)."""

REAL_GIT_FLAGS = ("--skip-analyze", "--no-remote", "--no-codegraph")
"""Forced on the real scanner: no LLM, no network, no CodeGraph binary."""

REAL_GIT_PHASES = ["Structural crawl", "Author identity"]
"""The phases the real scanner announces under :data:`REAL_GIT_FLAGS`."""

FAKE_KEY = "sk-ant-api03-simulatedE2EKey0123456789"
"""A provider-key-shaped secret the failure writes to stderr (redacted in the log)."""

_SCHEMA = """\
CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    docstring TEXT,
    signature TEXT
);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    line INTEGER
);
CREATE TABLE files (
    path TEXT PRIMARY KEY,
    language TEXT
);
"""


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def seed_codegraph(root: Path) -> None:
    """Write a two-function index whose symbol names are unique to ``root``."""
    ident = re.sub(r"\W+", "_", root.name).strip("_").lower() or "repo"
    path = f"src/{ident}.py"
    # (id, kind, name, qualified_name, start, end, signature)
    nodes = [
        ("n_file", "file", f"{ident}.py", path, 1, 12, None),
        (
            "n_main",
            "function",
            f"{ident}_main",
            f"{ident}.{ident}_main",
            1,
            6,
            f"def {ident}_main()",
        ),
        (
            "n_helper",
            "function",
            f"{ident}_helper",
            f"{ident}.{ident}_helper",
            8,
            12,
            f"def {ident}_helper()",
        ),
    ]
    target = root / ".codegraph" / "codegraph.db"
    target.parent.mkdir(exist_ok=True)
    target.unlink(missing_ok=True)
    conn = sqlite3.connect(str(target))
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, 'python', ?, ?, NULL, ?)",
            [
                (nid, kind, name, qname, path, start, end, sig)
                for nid, kind, name, qname, start, end, sig in nodes
            ],
        )
        conn.executemany(
            "INSERT INTO edges(source, target, kind) VALUES (?, ?, ?)",
            [
                ("n_file", "n_main", "contains"),
                ("n_file", "n_helper", "contains"),
                ("n_main", "n_helper", "calls"),
            ],
        )
        conn.execute("INSERT INTO files VALUES (?, 'python')", (path,))
        conn.commit()
    finally:
        conn.close()


PHASES = [
    "Structural crawl",
    "PR-origin recovery",
    "Author identity",
    "LLM descriptions",
]
"""Phase titles, in order, for the ``start`` event's ``phases`` list."""


def codegraph_only(fail: bool, delay: float) -> int:
    """The ``--codegraph-only`` run: one phase, only the CodeGraph index."""
    emit({"type": "start", "phase_total": 1, "phases": ["Code index"]})
    emit({"type": "phase", "phase": 1, "title": "CodeGraph"})
    emit({"type": "task", "name": "codegraph", "description": "indexing"})
    time.sleep(delay)
    if fail:
        sys.stderr.write("crawler 'codegraph' failed: simulated codegraph error\n")
    else:
        seed_codegraph(Path(os.getcwd()))
    emit(
        {
            "type": "result",
            "status": "failed" if fail else "ok",
            "elapsed_sec": 0.5,
            "phase_timings": {"CodeGraph": 0.5},
            "crawlers": [
                {
                    "name": "codegraph",
                    "status": "failed" if fail else "ok",
                    "summary": "" if fail else "synced",
                    **({"error": "simulated codegraph error"} if fail else {}),
                }
            ],
            "analyze_skipped": "--codegraph-only",
        }
    )
    return 1 if fail else 0


def forwarded(args: list[str]) -> list[str]:
    """``args`` without this fixture's own options, with :data:`REAL_GIT_FLAGS`."""
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in OWN_FLAGS:
            skip = True
            continue
        if arg in OWN_SWITCHES or arg.split("=", 1)[0] in OWN_FLAGS:
            continue
        out.append(arg)
    return out + [f for f in REAL_GIT_FLAGS if f not in out]


def real_git(args: list[str], fail: bool) -> int:
    """The ``--real-git`` run: the CodeGraph seed, then the real git crawl."""
    if fail:
        # The phases the real scanner announces with REAL_GIT_FLAGS; the git
        # crawler fails in the first, so the second never starts.
        emit({"type": "start", "phase_total": 2, "phases": REAL_GIT_PHASES})
        emit({"type": "phase", "phase": 1, "title": "Structural crawl"})
        sys.stderr.write("scan failed: simulated git error\n")
        emit(
            {
                "type": "result",
                "status": "failed",
                "elapsed_sec": 0.1,
                "phase_timings": {},
                "crawlers": [
                    {
                        "name": "git",
                        "status": "failed",
                        "summary": "",
                        "error": "simulated git error",
                    }
                ],
                "analyze_skipped": None,
            }
        )
        return 1
    # Seeded first: the real scanner runs with --no-codegraph, and the portal
    # may read the index as soon as the child's result event arrives.
    seed_codegraph(Path(os.getcwd()))
    cmd = [sys.executable, "-m", "whygraph", "scan", *forwarded(args)]
    return subprocess.run(cmd).returncode


def main() -> int:
    args = sys.argv[1:]
    control = Path(args[args.index("--control") + 1]) if "--control" in args else None
    delay = float(args[args.index("--delay") + 1]) if "--delay" in args else 0.15
    fail = False
    if control is not None:
        fail = (control / "fail").exists()
        if (control / "delay").exists():
            delay = float((control / "delay").read_text().strip())
    analyze = "--skip-analyze" not in args

    token_file = os.environ.get("WHYGRAPH_GITHUB_TOKEN_FILE")
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            token, problem = "", f"cannot read WHYGRAPH_GITHUB_TOKEN_FILE: {exc}"
        else:
            problem = "WHYGRAPH_GITHUB_TOKEN_FILE is empty"
        if not token:
            sys.stderr.write(f"scan failed: {problem}\n")
            emit({"type": "start", "phase_total": 3, "phases": PHASES[:3]})
            emit(
                {
                    "type": "result",
                    "status": "failed",
                    "elapsed_sec": 0.0,
                    "phase_timings": {},
                    "crawlers": [],
                    "analyze_skipped": None,
                    "error": problem,
                }
            )
            return 1

    if "--codegraph-only" in args:
        return codegraph_only(fail, delay)
    if "--real-git" in args:
        return real_git(args, fail)

    emit(
        {
            "type": "start",
            "phase_total": 4 if analyze else 3,
            "phases": PHASES if analyze else PHASES[:3],
        }
    )
    emit({"type": "phase", "phase": 1, "title": "Structural crawl"})
    for i in range(11):
        emit(
            {
                "type": "task",
                "name": "git",
                "completed": i,
                "total": 10,
                "description": f"walking commits {i}/10",
            }
        )
        time.sleep(delay)
    emit({"type": "task", "name": "codegraph", "description": "indexing"})

    if fail:
        # The failure belongs to phase 1 (the git crawler's); the later phases
        # never start, so the run page shows them skipped, not done.
        sys.stderr.write(f"scan failed: simulated crawler error with key {FAKE_KEY}\n")
        emit(
            {
                "type": "result",
                "status": "failed",
                "elapsed_sec": 1.0,
                "phase_timings": {"Structural crawl": 1.0},
                "crawlers": [
                    {
                        "name": "git",
                        "status": "failed",
                        "summary": "",
                        "error": "simulated crawler error",
                    },
                ],
                "analyze_skipped": None,
            }
        )
        return 1

    emit({"type": "phase", "phase": 2, "title": "PR-origin recovery"})
    time.sleep(delay)
    emit({"type": "phase", "phase": 3, "title": "Author identity"})
    time.sleep(delay)

    seed_codegraph(Path(os.getcwd()))
    if analyze:
        emit({"type": "phase", "phase": 4, "title": "LLM descriptions"})
        for i in range(11):
            emit(
                {
                    "type": "task",
                    "name": "analyze",
                    "completed": i,
                    "total": 10,
                    "description": "describing commits",
                }
            )
            time.sleep(delay)
    emit(
        {
            "type": "result",
            "status": "ok",
            "elapsed_sec": 2.5,
            "phase_timings": {
                "Structural crawl": 1.5,
                "PR-origin recovery": 0.1,
                "Author identity": 0.1,
                **({"LLM descriptions": 1.5} if analyze else {}),
            },
            "crawlers": [
                {"name": "git", "status": "ok", "summary": "2 commits (2 new)"},
                {"name": "codegraph", "status": "ok", "summary": "synced"},
            ],
            "analyze_skipped": None if analyze else "--skip-analyze",
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
