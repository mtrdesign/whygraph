"""A slow stand-in for ``whygraph scan --progress json``, for the Playwright suite.

Used through ``WHYGRAPH_SCAN_CMD="python3 tests/fixtures/e2e_scan.py --control DIR"``
(``make e2e`` sets it). Unlike ``fake_scan.py`` (instant, for runner tests) this
one takes a moment per phase so a browser can watch the progress bars move, and
it does what a real structural scan leaves behind: it writes a small CodeGraph
index into ``<cwd>/.codegraph/codegraph.db`` (the runner starts the child in the
project root). The symbols are named after the repo folder, so two projects
never share one - which is what the project-switch test relies on.

Options (before the runner's own ``--progress json --managed-by-portal ...``):

``--control DIR``   look for ``DIR/fail`` (exit 1 after phase 3, with a stderr
                    line carrying a fake key so redaction can be checked) and
                    ``DIR/delay`` (seconds per tick) on every run
``--delay SEC``     seconds per tick when ``DIR/delay`` is absent (default 0.15)

A production portal's runner passes a GitHub project's installation token
as a file (``WHYGRAPH_GITHUB_TOKEN_FILE``); when that variable is set the
file must be readable and non-empty, else the scan fails at once, as a real
scan's ``git`` / ``gh`` calls would.

Standard library only, so any ``python3`` will run it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

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
            emit({"type": "start", "phase_total": 3})
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

    emit({"type": "start", "phase_total": 4 if analyze else 3})
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
    emit({"type": "phase", "phase": 2, "title": "PR-origin recovery"})
    time.sleep(delay)
    emit({"type": "phase", "phase": 3, "title": "Author identity"})
    time.sleep(delay)

    if fail:
        sys.stderr.write(
            "scan failed: simulated crawler error with key sk-ant-secret-abcd\n"
        )
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
                    }
                ],
                "analyze_skipped": None,
            }
        )
        return 1

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
