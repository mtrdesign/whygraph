"""Rank Chat turns by the quality signals the serve layer already persists.

Usage::

    uv run python scripts/chat_signals.py <path/to/whygraph.db> \
        [--since-days 30] [--owner UID] [--out DIR]

Read-only: the database is opened with ``mode=ro`` and nothing is written
outside ``--out``. Meant for a local-mode project DB, whose sessions are the
developer's own; a DB with more than one session owner is refused unless
``--owner`` names one. The script imports nothing from ``whygraph``.

The output holds user questions and rating comments. Do not commit it until
each question is rewritten.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Copied from whygraph so the script needs no import of it.
REFUSAL_REASONS = ("refusal", "content_filter")  # services/llm/chat.py
TRUNCATED_FINISH_REASONS = ("max_tokens", "length")  # serve/chat.py
BUDGET_EXCEEDED = "budget_exceeded"  # core/usage.py
STOPPED = "Stopped."  # serve/chat.py (the user pressed Stop)

WARNING = "Contains user questions: do not commit until each question is rewritten."
TOP_N = 20


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _turns(conn: sqlite3.Connection, owner: str | None) -> list[dict]:
    sessions = {
        r["id"]: r
        for r in conn.execute("SELECT id, owner_uid FROM chat_session")
        if owner is None or r["owner_uid"] == owner
    }
    rows = [
        r
        for r in conn.execute("SELECT * FROM chat_message ORDER BY id")
        if r["session_id"] in sessions
    ]
    by_id = {r["id"]: r for r in rows}
    children: dict[int, list] = defaultdict(list)
    siblings: dict[tuple, list] = defaultdict(list)
    for r in rows:
        siblings[(r["session_id"], r["parent_id"], r["role"])].append(r)
        if r["parent_id"] is not None:
            children[r["parent_id"]].append(r)

    def descend(root) -> list:
        out, stack = [], [root]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(c for c in children[cur["id"]] if c["role"] != "user")
        return sorted(out, key=lambda r: r["id"])

    def ending(rows_: list):
        assistants = [r for r in rows_ if r["role"] == "assistant"]
        for r in reversed(assistants):
            kids = [c for c in children[r["id"]] if c["role"] in ("assistant", "tool")]
            if not kids and (r["tool_calls"] in ("[]", None) or r["error"]):
                return r
        return assistants[-1] if assistants else None

    turns = []
    for q in rows:
        if q["role"] != "user":
            continue
        edited = any(
            s["id"] > q["id"]
            for s in siblings[(q["session_id"], q["parent_id"], "user")]
        )
        for first in (c for c in children[q["id"]] if c["role"] == "assistant"):
            trows = descend(first)
            end = ending(trows)
            if end is None:
                continue
            regenerated = any(
                s["id"] > first["id"]
                for s in siblings[(q["session_id"], q["id"], "assistant")]
            )
            continued = any(
                r["role"] == "assistant"
                and r["parent_id"] in by_id
                and by_id[r["parent_id"]]["role"] == "assistant"
                and by_id[r["parent_id"]]["tool_calls"] == "[]"
                and by_id[r["parent_id"]]["error"] is None
                for r in trows
            )
            errors = [r["error"] for r in trows if r["error"]]
            stopped = STOPPED in errors
            budget = BUDGET_EXCEEDED in errors
            errored = any(e not in (STOPPED, BUDGET_EXCEEDED) for e in errors) or (
                end["finish_reason"] in REFUSAL_REASONS + TRUNCATED_FINISH_REASONS
            )
            turns.append(
                {
                    "session": q["session_id"],
                    "message": end["id"],
                    "mode": end["mode"] or q["mode"],
                    "model": end["model"],
                    "question": (q["content"] or "")[:200],
                    "rating": end["rating"],
                    "rating_comment": end["rating_comment"],
                    "regenerated": regenerated,
                    "edited": edited,
                    "continued": continued,
                    "stopped": stopped,
                    "budget_stopped": budget,
                    "errored": errored,
                    "tool_errors": sum(
                        1 for r in trows if r["role"] == "tool" and r["is_error"]
                    ),
                    "rounds": sum(1 for r in trows if r["role"] == "assistant"),
                    "_when": end["rated_at"] or end["created_at"],
                }
            )
    return turns


def _parse(stamp: str | None) -> datetime:
    if not stamp:
        return datetime.min.replace(tzinfo=timezone.utc)
    when = datetime.fromisoformat(stamp)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _score(t: dict) -> int:
    return (
        3 * (t["rating"] == -1)
        + 2 * t["regenerated"]
        + 2 * t["edited"]
        + 2 * t["errored"]
        + t["stopped"]
        + t["tool_errors"]
    )


def _flags(t: dict) -> str:
    names = [
        n
        for n in (
            "regenerated",
            "edited",
            "continued",
            "stopped",
            "budget_stopped",
            "errored",
        )
        if t[n]
    ]
    if t["rating"] == -1:
        names.insert(0, "down")
    elif t["rating"] == 1:
        names.insert(0, "up")
    if t["tool_errors"]:
        names.append(f"tool_errors={t['tool_errors']}")
    return ", ".join(names) or "-"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("db", type=Path)
    ap.add_argument("--since-days", type=int, default=30)
    ap.add_argument("--owner")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    conn = _connect(args.db)
    try:
        owners = {
            r[0] for r in conn.execute("SELECT DISTINCT owner_uid FROM chat_session")
        }
        owners.discard(None)
        if args.owner is None and len(owners) > 1:
            print(
                "refusing: this DB holds sessions of several owners; "
                "pass --owner UID to read one owner's sessions",
                file=sys.stderr,
            )
            return 2
        turns = _turns(conn, args.owner)
    finally:
        conn.close()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.since_days)
    turns = [t for t in turns if _parse(t["_when"]) >= cutoff]
    # stable: the highest score first, then the most recent turn
    ranked = sorted(turns, key=lambda t: (_score(t), t["message"]), reverse=True)
    worst = [t for t in ranked if _score(t) > 0][:TOP_N]

    out = args.out or Path(tempfile.gettempdir()) / "chat-signals"
    out.mkdir(parents=True, exist_ok=True)

    lines = [WARNING, "", f"# Chat signals ({len(turns)} turns)", ""]
    for name in (
        "regenerated",
        "edited",
        "continued",
        "stopped",
        "budget_stopped",
        "errored",
    ):
        lines.append(f"- {name}: {sum(1 for t in turns if t[name])}")
    lines.append(f"- down: {sum(1 for t in turns if t['rating'] == -1)}")
    lines.append(f"- up: {sum(1 for t in turns if t['rating'] == 1)}")
    lines.append(f"- tool_errors: {sum(t['tool_errors'] for t in turns)}")
    lines += ["", f"## Worst {len(worst)} turns", ""]
    for i, t in enumerate(worst, 1):
        lines.append(
            f"{i}. session {t['session']}, message {t['message']}, mode {t['mode']}, "
            f"model {t['model']}, score {_score(t)}: {_flags(t)}"
        )
        lines.append(f"   - {t['question']!r}")
    (out / "signals.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    cands = {
        "_note": WARNING,
        "candidates": [
            {
                "id": f"C{i}",
                "question": t["question"],
                "expect": {},
                "_source": {"session": t["session"], "message": t["message"]},
            }
            for i, t in enumerate(worst, 1)
        ],
    }
    (out / "candidates.json").write_text(
        json.dumps(cands, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {out / 'signals.md'} and {out / 'candidates.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
