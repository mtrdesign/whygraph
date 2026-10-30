"""Tests for ``scripts/chat_signals.py`` over a real migrated project DB."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from alembic import command

from whygraph import core
from whygraph.core.config import Config
from whygraph.db import engine as db_engine
from whygraph.db.bootstrap import alembic_config

_SPEC = importlib.util.spec_from_file_location(
    "chat_signals",
    Path(__file__).resolve().parent.parent / "scripts" / "chat_signals.py",
)
assert _SPEC and _SPEC.loader
signals = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(signals)

NOW = datetime.now(timezone.utc)


def _ts(days_ago: float = 0) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


@pytest.fixture(autouse=True)
def _isolate_config_and_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    db_path = tmp_path / "whygraph.db"
    monkeypatch.setattr(core, "_config", Config(whygraph_db=db_path))
    db_engine._reset_engine()
    try:
        yield db_path
    finally:
        db_engine._reset_engine()
        core._reset_config()


class _Db:
    def __init__(self, path: Path) -> None:
        command.upgrade(alembic_config(), "head")
        self.path = path
        self.conn = sqlite3.connect(path)

    def session(self, owner: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO chat_session (title, provider, model, owner_uid, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("t", "p", "m", owner, _ts(), _ts()),
        )
        return int(cur.lastrowid)

    def msg(self, session: int, role: str, parent: int | None, **kw) -> int:
        cols = {
            "session_id": session,
            "role": role,
            "content": kw.pop("content", f"{role} text"),
            "tool_calls": kw.pop("tool_calls", "[]"),
            "parent_id": parent,
            "created_at": kw.pop("created_at", _ts()),
        }
        cols.update(kw)
        cur = self.conn.execute(
            f"INSERT INTO chat_message ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            list(cols.values()),
        )
        return int(cur.lastrowid)

    def done(self) -> Path:
        self.conn.commit()
        self.conn.close()
        return self.path


def _run(db: Path, tmp_path: Path, *extra: str) -> tuple[int, str]:
    out = tmp_path / "out"
    rc = signals.main([str(db), "--out", str(out), *extra])
    md = out / "signals.md"
    return rc, md.read_text() if md.exists() else ""


def test_ranking_and_read_only(
    _isolate_config_and_engine: Path, tmp_path: Path
) -> None:
    d = _Db(_isolate_config_and_engine)
    s = d.session()
    # a plain good turn
    q1 = d.msg(s, "user", None, content="good question")
    d.msg(s, "assistant", q1, rating=1)
    # regenerated and down-rated: the replaced reply, then its replacement
    s = d.session()
    q2 = d.msg(s, "user", None, content="bad question")
    d.msg(s, "assistant", q2, rating=-1, rating_comment="nope")
    d.msg(s, "assistant", q2)
    db = d.done()
    before = (hashlib.sha256(db.read_bytes()).hexdigest(), db.stat().st_mtime_ns)

    rc, md = _run(db, tmp_path)
    assert rc == 0
    assert md.splitlines()[0].startswith("Contains user questions: do not commit")
    worst = md.split("## Worst")[1]
    assert "bad question" in worst and "good question" not in worst
    assert "down, regenerated" in worst
    assert "- regenerated: 1" in md
    cand = (tmp_path / "out" / "candidates.json").read_text()
    assert "do not commit" in cand and '"id": "C1"' in cand
    assert (
        hashlib.sha256(db.read_bytes()).hexdigest(),
        db.stat().st_mtime_ns,
    ) == before


def test_edited_continued_stopped_budget(
    _isolate_config_and_engine: Path, tmp_path: Path
) -> None:
    d = _Db(_isolate_config_and_engine)
    s = d.session()
    qa = d.msg(s, "user", None, content="first wording")
    d.msg(s, "assistant", qa)
    d.msg(s, "user", None, content="second wording")  # sibling: qa was edited
    s = d.session()
    q = d.msg(s, "user", None, content="chain")
    a1 = d.msg(s, "assistant", q, finish_reason="max_tokens")
    d.msg(s, "assistant", a1)  # continuation
    s = d.session()
    q3 = d.msg(s, "user", None, content="press stop")
    d.msg(s, "assistant", q3, error="Stopped.")
    s = d.session()
    q4 = d.msg(s, "user", None, content="over budget")
    d.msg(s, "assistant", q4, error="budget_exceeded")
    db = d.done()

    rc, md = _run(db, tmp_path)
    assert rc == 0
    assert "- edited: 1" in md
    assert "- continued: 1" in md
    assert "- stopped: 1" in md
    assert "- budget_stopped: 1" in md
    # budget_stopped is reported but not scored; the edited turn is the only
    # scored one besides the stopped turn
    worst = md.split("## Worst")[1]
    assert "over budget" not in worst
    assert "press stop" in worst and "first wording" in worst


def test_errored_refusal_and_truncated(
    _isolate_config_and_engine: Path, tmp_path: Path
) -> None:
    d = _Db(_isolate_config_and_engine)
    s = d.session()
    q1 = d.msg(s, "user", None, content="refused")
    d.msg(s, "assistant", q1, finish_reason="refusal")
    s = d.session()
    q2 = d.msg(s, "user", None, content="cut off")
    d.msg(s, "assistant", q2, finish_reason="max_tokens")
    s = d.session()
    q3 = d.msg(s, "user", None, content="fine")
    d.msg(s, "assistant", q3, finish_reason="stop")
    db = d.done()

    _, md = _run(db, tmp_path)
    assert "- errored: 2" in md
    worst = md.split("## Worst")[1]
    assert "refused" in worst and "cut off" in worst and "fine" not in worst


def test_since_days_uses_rated_at(
    _isolate_config_and_engine: Path, tmp_path: Path
) -> None:
    d = _Db(_isolate_config_and_engine)
    s = d.session()
    q1 = d.msg(s, "user", None, content="old but rated now", created_at=_ts(90))
    d.msg(s, "assistant", q1, created_at=_ts(90), rating=-1, rated_at=_ts(1))
    s = d.session()
    q2 = d.msg(s, "user", None, content="old and quiet", created_at=_ts(90))
    d.msg(s, "assistant", q2, created_at=_ts(90), rating=-1, rated_at=None)
    db = d.done()

    _, md = _run(db, tmp_path, "--since-days", "30")
    assert "old but rated now" in md
    assert "old and quiet" not in md


def test_multi_owner_refused_without_owner(
    _isolate_config_and_engine: Path, tmp_path: Path
) -> None:
    d = _Db(_isolate_config_and_engine)
    for owner, text in (("u1", "mine"), ("u2", "theirs")):
        s = d.session(owner)
        q = d.msg(s, "user", None, content=text)
        d.msg(s, "assistant", q, rating=-1)
    db = d.done()

    rc, md = _run(db, tmp_path)
    assert rc == 2 and md == ""
    rc, md = _run(db, tmp_path, "--owner", "u1")
    assert rc == 0 and "mine" in md and "theirs" not in md
