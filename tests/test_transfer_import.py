"""Tests for the project transfer import (plan 42)."""

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import insert
from sqlmodel import select

from test_portal_app import env  # noqa: F401 -- fixture
from whygraph.analyze.backfill import import_descriptions
from whygraph.core.config import Config
from whygraph.core.context import ProjectContext, use_project
from whygraph.core.import_checks import ImportFormatError
from whygraph.db import ensure_initialized, get_session
from whygraph.db.models import (
    Commit,
    CommitFileChange,
    ProjectPurposeRun,
    RationaleCache,
)
from whygraph.mcp.rationale_cache import import_rows, remove_imported
from whygraph.portal.purpose_routes import import_runs
from whygraph.portal.transfer_import import transfer_config_layer

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
T0 = "2026-09-01T10:00:00+00:00"
FP = "c" * 64
KEY = ("openai", "default")


@pytest.fixture
def project(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """A migrated WhyGraph DB bound as the current project."""
    db = tmp_path / "whygraph.db"
    ctx = ProjectContext(slug="demo", root=tmp_path, config=Config(whygraph_db=db))
    with use_project(ctx):
        ensure_initialized()
        yield SimpleNamespace(db=db)


def _card(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "path": "src/a.py",
        "line_start": 1,
        "line_end": 10,
        "provider": "openai",
        "model": "default",
        "evidence_fingerprint": FP,
        "cached_at": T0,
        "purpose": "p",
        "why": "w",
        "constraints": ["c"],
        "tradeoffs": [],
        "risks": [],
        "input_tokens": 1,
        "output_tokens": 2,
        "actual_provider": "openai",
        "actual_model": "gpt",
        "qualified_name": None,
    }
    return {**base, **kw}


def _rows(records, **kw: Any):
    with get_session() as session:
        return import_rows(
            session,
            records,
            import_id=7,
            now=NOW,
            cache_key=kw.pop("cache_key", KEY),
        )


def test_rationale_inserts_stamps_and_target_wins(project):
    first = _rows([_card(), _card(line_start=2)])
    assert (first.inserted, first.existing) == (2, 0)
    with get_session() as session:
        rows = session.exec(select(RationaleCache)).all()
        assert {r.import_id for r in rows} == {7}
        assert json.loads(rows[0].constraints) == ["c"]
        row = session.get(RationaleCache, ("src/a.py", 1, 10, "openai", "default"))
        row.purpose = "mine"
        session.add(row)
    again = _rows([_card(purpose="theirs"), _card(line_start=3)])
    assert (again.inserted, again.existing) == (1, 1)
    with get_session() as session:
        row = session.get(RationaleCache, ("src/a.py", 1, 10, "openai", "default"))
        assert row.purpose == "mine"


def test_rationale_duplicate_in_bundle_counts_existing(project):
    result = _rows([_card(), _card(purpose="second")])
    assert (result.inserted, result.existing) == (1, 1)


def test_rationale_inactive_model_uses_model_key(project):
    result = _rows(
        [_card(), _card(model="gpt-x", line_start=5), _card(provider="other")]
    )
    assert result.inserted == 3 and result.inactive_model == 2
    assert (
        _rows([_card(model="gpt-x")], cache_key=("openai", "gpt-x")).inactive_model == 0
    )


def test_rationale_skips(project):
    long_list = ["x"] * 51
    result = _rows(
        [
            _card(purpose="x" * 32_001),
            _card(constraints=long_list),
            _card(risks=["x" * 4_001]),
            _card(line_start=0),
            _card(line_start=9, line_end=3),
            _card(line_start=1, line_end=5_002),
            _card(path=".env"),
            _card(path=".GIT/config"),
            _card(line_start=5),
        ]
    )
    assert result.inserted == 1
    assert result.skipped_oversize == 6
    assert result.skipped_denied == 2


def test_rationale_format_errors_refuse(project):
    bad = [
        _card(path="../x"),
        _card(path="/abs"),
        _card(evidence_fingerprint="XYZ"),
        _card(cached_at="2030-01-01T00:00:00+00:00"),
        _card(cached_at="nope"),
        _card(provider="a\nb".replace("\\n", "\n")),
        _card(line_start="1"),
        _card(constraints="not a list"),
        _card(purpose=3),
    ]
    bad[5] = _card(provider="a\nb")
    for record in bad:
        with pytest.raises(ImportFormatError):
            _rows([_card(), record])
    with pytest.raises(ImportFormatError) as info:
        _rows([_card(), _card(path="../x")])
    assert info.value.index == 1
    with get_session() as session:
        assert session.exec(select(RationaleCache)).all() == []


def test_rationale_control_chars_replaced(project):
    _rows([_card(purpose="a\x00b")])
    with get_session() as session:
        assert session.exec(select(RationaleCache.purpose)).one() == "a\ufffdb"


def test_remove_imported_keeps_native_rows(project):
    _rows([_card(), _card(line_start=2)])
    with get_session() as session:
        session.add(
            RationaleCache(
                **{
                    **_card(line_start=3),
                    "constraints": "[]",
                    "tradeoffs": "[]",
                    "risks": "[]",
                }
            )
        )
    with get_session() as session:
        assert remove_imported(session) == 2
    with get_session() as session:
        rows = session.exec(select(RationaleCache)).all()
        assert [r.line_start for r in rows] == [3]


def test_rationale_dry_run_rollback_leaves_nothing(project):
    with get_session() as session:
        import_rows(session, [_card()], import_id=1, now=NOW, cache_key=KEY)
        session.rollback()
    with get_session() as session:
        assert session.exec(select(RationaleCache)).all() == []


def test_rationale_inserts_many_rows_in_one_call(project):
    records = [
        _card(path=f"src/f{i % 500}.py", line_start=i + 1, line_end=i + 3)
        for i in range(5_000)
    ]
    assert _rows(records).inserted == 5_000


# --- purpose runs ---


def _run(note_id: int | None = 1, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "requested_by": "u1",
        "started_at": T0,
        "finished_at": T0,
        "status": "ok",
        "input_hash": FP,
        "input_bytes": 10,
        "sources": ["README.md"],
        "provider": "openai",
        "model": "m",
        "tokens_in": 1,
        "tokens_out": 2,
        "note_id": note_id,
        "error_code": None,
    }
    return {**base, **kw}


def _runs(records, note_ids):
    with get_session() as session:
        return import_runs(session, records, note_ids=note_ids, now=NOW)


def test_purpose_runs_remap_prefix_and_skip(project):
    count = _runs([_run(1), _run(2), _run(None), _run(1, requested_by="")], {1: 41})
    assert count == 2
    with get_session() as session:
        rows = session.exec(select(ProjectPurposeRun)).all()
        assert {(r.note_id, r.requested_by) for r in rows} == {
            (41, "imported:u1"),
            (41, ""),
        }
        assert json.loads(rows[0].sources) == ["README.md"]


def test_purpose_runs_reimport_with_no_inserted_notes_adds_none(project):
    assert _runs([_run(1)], {}) == 0


def test_purpose_runs_format_errors(project):
    for bad in (
        _run(status="weird"),
        _run(requested_by="imported:u"),
        _run(started_at="2031-01-01T00:00:00+00:00"),
        _run(input_hash="zz"),
        _run(sources="x"),
        _run(sources=["a"] * 21),
        _run(tokens_in=-1),
        _run(model="x" * 201),
    ):
        with pytest.raises(ImportFormatError):
            _runs([bad], {1: 1})
    with get_session() as session:
        assert session.exec(select(ProjectPurposeRun)).all() == []


# --- descriptions ---


def _commit_row(sha: str, desc: str | None = None, model: str | None = None):
    return {
        "sha": sha,
        "parent_shas": "",
        "author_name": "a",
        "author_email": "a@x",
        "authored_at": T0,
        "committed_at": T0,
        "subject": "s",
        "body": "",
        "files_changed": 1,
        "insertions": 1,
        "deletions": 0,
        "scanned_at": T0,
        "llm_description": desc,
        "llm_description_model": model,
    }


def sha(n: int) -> str:
    return f"{n:040x}"


def test_descriptions_fill_only_null(project):
    with get_session() as session:
        session.connection().execute(
            insert(Commit),
            [
                _commit_row(sha(1)),
                _commit_row(sha(2), "mine", "openai:x"),
                _commit_row(sha(3), "stub", None),
            ],
        )
        session.add(CommitFileChange(commit_sha=sha(1), path="a.py", change_type="M"))
        session.add(
            CommitFileChange(
                commit_sha=sha(1),
                path="b.py",
                change_type="M",
                llm_description="keep",
                llm_description_model="o:m",
            )
        )
    commits = [
        {"sha": sha(n), "llm_description": f"d{n}", "llm_description_model": "o:m"}
        for n in (1, 2, 3, 9)
    ]
    files = [
        {
            "commit_sha": sha(1),
            "path": p,
            "llm_description": "f",
            "llm_description_model": "o:m",
        }
        for p in ("a.py", "b.py", "zzz.py")
    ]
    with get_session() as session:
        result = import_descriptions(session, commits, files)
    assert result.as_dict() == {
        "filled": 2,
        "kept_target": 3,
        "missing_commit": 2,
        "skipped_oversize": 0,
    }
    with get_session() as session:
        by = {c.sha: c for c in session.exec(select(Commit)).all()}
        assert by[sha(1)].llm_description == "d1"
        assert by[sha(1)].llm_description_model == "o:m"
        assert by[sha(2)].llm_description == "mine"
        assert by[sha(3)].llm_description == "stub"
        files_by = {f.path: f for f in session.exec(select(CommitFileChange)).all()}
        assert files_by["a.py"].llm_description == "f"
        assert files_by["b.py"].llm_description == "keep"
        # idempotent
        again = import_descriptions(session, commits, files)
        assert again.filled == 0


def test_descriptions_oversize_and_format(project):
    with get_session() as session:
        session.connection().execute(insert(Commit), [_commit_row(sha(1))])
        big = {
            "sha": sha(1),
            "llm_description": "x" * 64_001,
            "llm_description_model": "o:m",
        }
        result = import_descriptions(session, [big], [])
        assert result.skipped_oversize == 1 and result.filled == 0
        for bad in (
            {"sha": "ABC", "llm_description": "x", "llm_description_model": "o:m"},
            {"sha": sha(1), "llm_description": "x", "llm_description_model": "nocolon"},
            {"sha": sha(1), "llm_description": "x", "llm_description_model": "o:m\n"},
            {"sha": sha(1), "llm_description": 5, "llm_description_model": "o:m"},
        ):
            with pytest.raises(ImportFormatError):
                import_descriptions(session, [bad], [])
        with pytest.raises(ImportFormatError):
            import_descriptions(
                session,
                [],
                [
                    {
                        "commit_sha": sha(1),
                        "path": "../x",
                        "llm_description": "x",
                        "llm_description_model": "o:m",
                    }
                ]
                if False
                else [
                    {
                        "commit_sha": sha(1),
                        "path": "",
                        "llm_description": "x",
                        "llm_description_model": "o:m",
                    }
                ],
            )


def test_descriptions_fill_many_rows_in_one_call(project):
    n_commits, per = 20_000, 2
    with get_session() as session:
        conn = session.connection()
        conn.execute(insert(Commit), [_commit_row(sha(i)) for i in range(n_commits)])
        conn.execute(
            insert(CommitFileChange),
            [
                {
                    "commit_sha": sha(i // per),
                    "path": f"f{i % per}.py",
                    "change_type": "M",
                }
                for i in range(40_000)
            ],
        )
    commits = [
        {"sha": sha(i), "llm_description": "d" * 400, "llm_description_model": "o:m"}
        for i in range(n_commits)
    ]
    files = [
        {
            "commit_sha": sha(i // per),
            "path": f"f{i % per}.py",
            "llm_description": "f" * 300,
            "llm_description_model": "o:m",
        }
        for i in range(40_000)
    ]
    with get_session() as session:
        result = import_descriptions(session, commits, files)
    assert result.filled == 60_000


LAYER = {
    "llm": {
        "model": "m",
        "api_key": "x",
        "openai": {"model": "o", "base_url": "http://h"},
    },
    "analyze": {"agent_descriptions_per_hour": 5},
    "rationale": {"agent_generations_per_hour": 5},
    "chat": {"mode": "lead"},
    "areas": {"core": {"paths": ["src/"]}},
    "scan": {"hooks": True, "forge": "github"},
}


def test_keeps_model_and_knowledge_keys_only():
    for mode in ("local", "production"):
        kept = transfer_config_layer(LAYER, mode)
        assert kept == {
            "llm": {"model": "m", "openai": {"model": "o"}},
            "chat": {"mode": "lead"},
            "areas": {"core": {"paths": ["src/"]}},
        }


def test_empty_layer():
    assert transfer_config_layer({}, "local") == {}


def test_null_tables_are_dropped_but_null_leaves_stay():
    layer = {
        "llm": None,
        "analyze": None,
        "chat": {"mode": None},
        "areas": None,
    }
    for mode in ("local", "production"):
        assert transfer_config_layer(layer, mode) == {
            "chat": {"mode": None},
            "areas": None,  # a leaf: resets only the transferable key
        }
        assert transfer_config_layer({"llm": {"openai": None}}, mode) == {}
        assert transfer_config_layer({"llm": {"model": "m", "openai": None}}, mode) == {
            "llm": {"model": "m"}
        }


# ---------------------------------------------------------------------------
# The orchestrator: apply_import (plan 42 section 5.7, step 8)
# ---------------------------------------------------------------------------

ROOT_SHA = sha(1)
OTHER_SHA = sha(2)
IMPORTER = "importer-uid"
WIDE_BODY = "Billing is the one service that may write to the ledger."


@pytest.fixture
def target(tmp_path: Path) -> Iterator[SimpleNamespace]:
    """A scanned target: a data-dir WhyGraph DB with a root and a child commit."""
    from whygraph.portal import db as portal_db
    from whygraph.portal.paths import project_db_path

    data = portal_db.data_dir()
    db = project_db_path(data, "local", "demo")
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    ctx = ProjectContext(slug="demo", root=root, config=Config(whygraph_db=db))
    with use_project(ctx):
        ensure_initialized()
        with get_session() as session:
            session.connection().execute(
                insert(Commit),
                [
                    _commit_row(ROOT_SHA),
                    {**_commit_row(OTHER_SHA), "parent_shas": ROOT_SHA},
                ],
            )
    row = SimpleNamespace(
        last_scanned_head=OTHER_SHA, remote_url=None, source="local", root=str(root)
    )
    bound = SimpleNamespace(
        id=1, org_id=1, slug="demo", root=root, ctx=ctx, db_path=db, source="local"
    )
    yield SimpleNamespace(data=data, db=db, root=root, ctx=ctx, row=row, bound=bound)


def _note(note_id: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": note_id,
        "kind": "decision",
        "body": f"note {note_id}",
        "edited_on_confirm": False,
        "status": "confirmed",
        "anchor_kind": "range",
        "anchor_path": "src/a.py",
        "line_start": 1,
        "line_end": 1,
        "qualified_name": None,
        "area": None,
        "content_hash": None,
        "source": "human",
        "origin": "ui",
        "refs": [ROOT_SHA],
        "supersedes_id": None,
        "proposed_by": "src-ada",
        "proposed_at": T0,
        "confirmed_by": "src-ada",
        "confirmed_at": T0,
    }
    return {**base, **kw}


def _chat_session(sid: int, **kw: Any) -> dict[str, Any]:
    import hashlib

    base: dict[str, Any] = {
        "id": sid,
        "title": f"Chat {sid}",
        "provider": "anthropic",
        "model": "claude-x",
        "created_at": T0,
        "updated_at": T0,
        "active_leaf_id": None,
        "effort": None,
        "mode": None,
        "origin_key": hashlib.sha256(f"origin-{sid}".encode()).hexdigest(),
    }
    return {**base, **kw}


def _chat_message(mid: int, sid: int, parent: int | None, **kw: Any) -> dict:
    return {
        "id": mid,
        "session_id": sid,
        "parent_id": parent,
        "role": "user",
        "content": f"message {mid}",
        "created_at": T0,
        **kw,
    }


def _members(**overrides: Any) -> dict[str, Any]:
    """A bundle with one of everything; ``overrides`` replace whole members."""
    from whygraph.portal import transfer_bundle as tb

    members: dict[str, Any] = {
        tb.ACTORS: [{"uid": "src-ada", "display_name": "Ada"}],
        tb.NOTES: [
            _note(
                1,
                kind="constraint",
                anchor_kind="project",
                body=WIDE_BODY,
                anchor_path=None,
                line_start=None,
                line_end=None,
            ),
            _note(2),
            _note(
                3,
                kind="purpose",
                source="analysis",
                anchor_kind="project",
                body="A billing service.",
                anchor_path=None,
                line_start=None,
                line_end=None,
                origin=None,
                refs=None,
            ),
        ],
        tb.PURPOSE_RUNS: [_run(3)],
        tb.RATIONALE: [_card()],
        tb.COMMIT_DESCRIPTIONS: [
            {"sha": ROOT_SHA, "llm_description": "d", "llm_description_model": "o:m"}
        ],
        tb.FILE_DESCRIPTIONS: [],
        tb.CHAT_SESSIONS: [_chat_session(1, active_leaf_id=2)],
        tb.CHAT_MESSAGES: [
            _chat_message(1, 1, None),
            _chat_message(2, 1, 1, role="assistant", content="answer"),
        ],
    }
    members.update(overrides)
    return members


def _bundle(path: Path, members: dict[str, Any], *, roots=(ROOT_SHA,)) -> Path:
    from whygraph.portal import transfer_bundle as tb

    manifest = tb.Manifest(
        format=tb.FORMAT,
        format_version=tb.FORMAT_VERSION,
        whygraph_version="2.2.1",
        exported_at=T0,
        source=tb.ManifestSource(mode="local", label="local/demo", name="demo"),
        repo=tb.ManifestRepo(root_commits=list(roots), origin="github.com/a/b"),
    )
    wire = {}
    for name, value in members.items():
        if name == tb.CONFIG:
            wire[name] = value
        else:
            model = tb.MEMBER_MODELS[name]
            wire[name] = [model.model_validate(r) for r in value]
    tb.write_bundle(path, manifest, wire)
    return path


def _import(t: SimpleNamespace, path: Path, **kw: Any) -> dict[str, Any]:
    from whygraph.portal import transfer_bundle as tb
    from whygraph.portal.transfer_import import apply_import

    options: dict[str, Any] = {
        "uid": IMPORTER,
        "keep_status": True,
        "apply_config": False,
        "dry_run": False,
        "mode": "local",
        "state": None,
    }
    options.update(kw)
    with tb.read_bundle(path) as bundle:
        return apply_import(t.row, t.bound, bundle, **options)


def _dump_hash(db: Path) -> str:
    import hashlib
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return hashlib.sha256("\n".join(conn.iterdump()).encode()).hexdigest()
    finally:
        conn.close()


def _counts_only(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k not in ("applied", "backup")}


def test_apply_imports_every_member(target, tmp_path):
    from whygraph.db.models import (
        ChatMessage,
        ChatSession,
        ImportedActor,
        ImportedChat,
        ProjectImport,
        ProjectNote,
    )

    result = _import(target, _bundle(tmp_path / "b.zip", _members()))
    assert result["applied"] is True
    assert result["source"] == {
        "mode": "local",
        "label": "local/demo",
        "whygraph_version": "2.2.1",
        "exported_at": T0,
    }
    assert result["repo"] == {
        "matched_roots": 1,
        "source_origin": "github.com/a/b",
        "target_origin": None,
    }
    assert result["notes"]["inserted"] == {"proposed": 0, "confirmed": 3, "retired": 0}
    assert result["purpose_runs"] == {"inserted": 1}
    assert result["rationale"]["inserted"] == 1
    assert result["descriptions"]["filled"] == 1
    assert result["chats"] == {
        "sessions": 1,
        "matched_existing": 0,
        "skipped_oversize": 0,
        "messages": 2,
        "providers": ["anthropic"],
    }
    assert result["config"] == {
        "present": False,
        "invalid": [],
        "changes": [],
        "applied": False,
        "error": None,
    }
    # Q5: the project-anchored constraint and the purpose card, not the range note.
    assert [(w["kind"], w["body"]) for w in result["wide_confirmed"]] == [
        ("constraint", WIDE_BODY),
        ("purpose", "A billing service."),
    ]
    with use_project(target.ctx), get_session() as session:
        log = session.exec(select(ProjectImport)).one()
        assert log.imported_by == IMPORTER and log.source_label == "local/demo"
        assert json.loads(log.counts)["notes"]["inserted"]["confirmed"] == 3
        actor = session.exec(select(ImportedActor)).one()
        assert (actor.uid, actor.display_name, actor.import_id) == (
            "imported:src-ada",
            "Ada",
            log.id,
        )
        assert {n.confirmed_by for n in session.exec(select(ProjectNote))} == {
            "imported:src-ada"
        }
        chat = session.exec(select(ChatSession)).one()
        assert chat.owner_uid == IMPORTER
        assert len(session.exec(select(ChatMessage)).all()) == 2
        assert session.exec(select(ImportedChat)).one().import_id == log.id
        assert session.exec(select(RationaleCache)).one().import_id == log.id
        assert session.get(Commit, ROOT_SHA).llm_description == "d"


def test_backup_written_0600_and_path_only_in_local_mode(target, tmp_path):
    path = _bundle(tmp_path / "b.zip", _members())
    local = _import(target, path)
    assert local["backup"]["written"] and not local["backup"]["skipped_low_disk"]
    backup = Path(local["backup"]["path"])
    assert backup.parent == target.db.parent / "backups"
    assert backup.name.startswith("whygraph-import.") and backup.suffix == ".db"
    assert backup.stat().st_mode & 0o777 == 0o600
    production = _import(target, path, mode="production")
    assert production["backup"] == {
        "written": True,
        "skipped_low_disk": False,
        "path": None,
    }


def test_dry_run_is_the_apply_rolled_back(target, tmp_path):
    path = _bundle(tmp_path / "b.zip", _members())
    before = _dump_hash(target.db)
    dry = _import(target, path, dry_run=True)
    assert _dump_hash(target.db) == before
    assert dry["applied"] is False
    assert dry["backup"] == {"written": False, "skipped_low_disk": False, "path": None}
    assert not (target.db.parent / "backups").exists()
    applied = _import(target, path)
    assert _counts_only(applied) == _counts_only(dry)


def test_reimport_adds_nothing(target, tmp_path):
    path = _bundle(tmp_path / "b.zip", _members())
    _import(target, path)
    again = _import(target, path)
    assert again["notes"]["inserted"] == {"proposed": 0, "confirmed": 0, "retired": 0}
    # The imported purpose card is live now, so D11 skips the bundle's.
    assert again["notes"]["skipped_duplicate"] == 2
    assert again["notes"]["skipped_purpose"] == 1
    assert again["purpose_runs"] == {"inserted": 0}
    assert again["rationale"]["inserted"] == 0
    assert again["rationale"]["existing"] == 1
    assert again["descriptions"]["filled"] == 0
    assert again["chats"]["sessions"] == 0
    assert again["chats"]["matched_existing"] == 1
    assert again["wide_confirmed"] == []


def test_needs_a_scanned_target(target, tmp_path):
    from whygraph.portal.transfer_bundle import BundleError

    path = _bundle(tmp_path / "b.zip", _members())
    target.row.last_scanned_head = None
    with pytest.raises(BundleError) as exc:
        _import(target, path)
    assert (exc.value.status, exc.value.code) == (409, "transfer_needs_scan")
    target.row.last_scanned_head = OTHER_SHA
    with use_project(target.ctx), get_session() as session:
        session.exec(  # type: ignore[call-overload]
            Commit.__table__.delete()
        )
    with pytest.raises(BundleError) as exc:
        _import(target, path)
    assert exc.value.code == "transfer_needs_scan"


def test_wrong_repository_changes_nothing(target, tmp_path):
    from whygraph.portal.transfer_bundle import BundleError

    path = _bundle(tmp_path / "b.zip", _members(), roots=(sha(99),))
    before = _dump_hash(target.db)
    with pytest.raises(BundleError) as exc:
        _import(target, path)
    assert (exc.value.status, exc.value.code) == (409, "bundle_wrong_repo")
    assert "github.com/a/b" in exc.value.args[0]
    assert "shallow" in exc.value.args[0]
    assert _dump_hash(target.db) == before
    assert not (target.db.parent / "backups").exists()


def test_a_shallow_graft_point_passes_the_gate(target, tmp_path):
    # The bundle's own roots include a commit that is not a root here.
    path = _bundle(tmp_path / "b.zip", _members(), roots=(sha(98), OTHER_SHA))
    assert _import(target, path)["repo"]["matched_roots"] == 1


@pytest.mark.parametrize(
    ("member", "records", "where"),
    [
        (
            "file_descriptions.jsonl",
            [
                {
                    "commit_sha": ROOT_SHA,
                    "path": "ok.py",
                    "llm_description": "d",
                    "llm_description_model": "o:m",
                },
                {
                    "commit_sha": ROOT_SHA,
                    "path": "a.py",
                    "llm_description": "d",
                    "llm_description_model": "no model label",
                },
            ],
            "file_descriptions.jsonl line 2",
        ),
        (
            "chat_messages.jsonl",
            [_chat_message(1, 1, None), _chat_message(2, 1, 1, role="robot")],
            "chat_messages.jsonl line 2",
        ),
        (
            "actors.jsonl",
            [
                {"uid": "a", "display_name": "A"},
                {"uid": "imported:b", "display_name": "B"},
            ],
            "actors.jsonl line 2",
        ),
        (
            "notes.jsonl",
            [_note(1), _note(2, kind="secret")],
            "notes.jsonl line 2",
        ),
        (
            "rationale.jsonl",
            [_card(), _card(path="../x")],
            "rationale.jsonl line 2",
        ),
        (
            "purpose_runs.jsonl",
            [_run(3, status="weird")],
            "purpose_runs.jsonl line 1",
        ),
        (
            "commit_descriptions.jsonl",
            [{"sha": ROOT_SHA, "llm_description": "d", "llm_description_model": "x"}],
            "commit_descriptions.jsonl line 1",
        ),
    ],
)
def test_a_format_violation_refuses_and_writes_nothing(
    target, tmp_path, member, records, where
):
    from whygraph.portal.transfer_bundle import BundleError

    path = _bundle(tmp_path / "b.zip", _members(**{member: records}))
    before = _dump_hash(target.db)
    with pytest.raises(BundleError) as exc:
        _import(target, path)
    assert (exc.value.status, exc.value.code) == (400, "bundle_invalid")
    assert where in exc.value.args[0]
    assert _dump_hash(target.db) == before
    assert not (target.db.parent / "backups").exists()  # checked before the backup


def test_bundle_level_note_error_names_the_member(target, tmp_path):
    from whygraph.portal.transfer_bundle import BundleError

    notes = [_note(1, supersedes_id=5), _note(5)]
    path = _bundle(tmp_path / "b.zip", _members(**{"notes.jsonl": notes}))
    with pytest.raises(BundleError) as exc:
        _import(target, path)
    assert "notes.jsonl line 1: supersedes_id" in exc.value.args[0]


def test_purpose_skipped_when_the_target_has_one(target, tmp_path):
    from whygraph.db.models import ProjectNote, ProjectPurposeRun

    with use_project(target.ctx), get_session() as session:
        session.add(
            ProjectNote(
                kind="purpose",
                body="Ours.",
                status="proposed",
                anchor_kind="project",
                source="analysis",
                proposed_by="local-uid",
                proposed_at=T0,
            )
        )
    result = _import(target, _bundle(tmp_path / "b.zip", _members()))
    assert result["notes"]["skipped_purpose"] == 1
    assert result["purpose_runs"] == {"inserted": 0}
    assert [w["kind"] for w in result["wide_confirmed"]] == ["constraint"]
    with use_project(target.ctx), get_session() as session:
        assert session.exec(select(ProjectPurposeRun)).all() == []


def test_wide_notes_include_unresolved_areas_and_wide_paths(target, tmp_path):
    notes = [
        _note(
            1,
            anchor_kind="area",
            area="billing",
            anchor_path=None,
            line_start=None,
            line_end=None,
            body="area note",
        ),
        _note(
            2,
            anchor_kind="path",
            anchor_path="src",
            line_start=None,
            line_end=None,
            body="folder note",
        ),
        _note(
            3,
            anchor_kind="path",
            anchor_path="src/a.py",
            line_start=None,
            line_end=None,
            body="file note",
        ),
        _note(
            4,
            status="proposed",
            confirmed_by=None,
            confirmed_at=None,
            anchor_kind="project",
            anchor_path=None,
            line_start=None,
            line_end=None,
            body="proposed note",
        ),
    ]
    result = _import(
        target, _bundle(tmp_path / "b.zip", _members(**{"notes.jsonl": notes}))
    )
    assert [
        (w["anchor"], w["target"], w["body"], w["unresolved_area"])
        for w in result["wide_confirmed"]
    ] == [
        ("area", "billing", "area note", True),
        ("path", "src", "folder note", False),
    ]


def test_keep_status_false_lands_notes_proposed(target, tmp_path):
    result = _import(target, _bundle(tmp_path / "b.zip", _members()), keep_status=False)
    assert result["notes"]["downgraded"] == 3
    assert result["notes"]["inserted"]["confirmed"] == 0
    assert result["wide_confirmed"] == []


def test_inactive_model_uses_the_current_config(target, tmp_path):
    cards = [_card(provider="anthropic"), _card(provider="openai", line_start=4)]
    result = _import(
        target, _bundle(tmp_path / "b.zip", _members(**{"rationale.jsonl": cards}))
    )
    provider, _ = target.ctx.config.cache_identity("rationale")
    assert result["rationale"]["inactive_model"] == sum(
        1 for c in cards if c["provider"] != provider
    )


def test_actor_names_are_trimmed_and_first_wins(target, tmp_path):
    from whygraph.db.models import ImportedActor

    actors = [
        {"uid": "src-ada", "display_name": "  " + "A" * 150},
        {"uid": "src-ada", "display_name": "Second"},
    ]
    _import(target, _bundle(tmp_path / "b.zip", _members(**{"actors.jsonl": actors})))
    with use_project(target.ctx), get_session() as session:
        row = session.exec(select(ImportedActor)).one()
        assert row.display_name == "A" * 100


def test_actor_names_drop_bidi_marks_and_controls(target, tmp_path):
    from whygraph.db.models import ImportedActor

    # A real Hebrew name can carry an RLM; it must import, not refuse the bundle.
    actors = [{"uid": "src-dana", "display_name": "דנה‏ K‮\x07"}]
    _import(target, _bundle(tmp_path / "b.zip", _members(**{"actors.jsonl": actors})))
    with use_project(target.ctx), get_session() as session:
        row = session.exec(select(ImportedActor)).one()
        assert row.display_name == "דנה K"


def test_backup_retention_low_disk_and_symlinks(target, tmp_path, monkeypatch):
    import shutil as _shutil

    from whygraph.core.safe_paths import UnsafePathError
    from whygraph.portal import transfer_import
    from whygraph.portal.transfer_import import backup_project_db

    stamps = [datetime(2026, 11, 1, 10, 0, 0, i, tzinfo=timezone.utc) for i in range(5)]
    paths = [backup_project_db(target.db, target.data, now=s).path for s in stamps]
    kept = sorted(p.name for p in (target.db.parent / "backups").iterdir())
    assert kept == sorted(p.name for p in paths[-3:])

    usage = _shutil.disk_usage(target.db.parent)
    monkeypatch.setattr(
        transfer_import.shutil,
        "disk_usage",
        lambda _p: type(usage)(usage.total, usage.used, 10),
    )
    low = backup_project_db(target.db, target.data)
    assert (low.written, low.skipped_low_disk, low.path) == (False, True, None)
    monkeypatch.undo()

    _shutil.rmtree(target.db.parent / "backups")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (target.db.parent / "backups").symlink_to(elsewhere)
    with pytest.raises(UnsafePathError):
        backup_project_db(target.db, target.data)
    assert list(elsewhere.iterdir()) == []


def test_symlinked_backups_refuses_the_apply(target, tmp_path):
    from whygraph.portal.deps import ApiError

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (target.db.parent / "backups").symlink_to(elsewhere)
    path = _bundle(tmp_path / "b.zip", _members())
    before = _dump_hash(target.db)
    with pytest.raises(ApiError) as exc:
        _import(target, path)
    assert (exc.value.status, exc.value.code) == (409, "unsafe_path")
    assert _dump_hash(target.db) == before


# --- the cache batches (plan 42 Q11) ---


def _many_commits(t: SimpleNamespace, shas: list[str], paths: list[str]) -> None:
    """Commit rows (and a file row per path) on the target, children of the root."""
    with use_project(t.ctx), get_session() as session:
        session.connection().execute(
            insert(Commit),
            [{**_commit_row(s), "parent_shas": ROOT_SHA} for s in shas],
        )
        session.connection().execute(
            insert(CommitFileChange),
            [
                {"commit_sha": s, "path": p, "change_type": "M"}
                for s in shas
                for p in paths
            ],
        )


def _desc(s: str, text: str = "d") -> dict[str, Any]:
    return {"sha": s, "llm_description": text, "llm_description_model": "o:m"}


def _file_desc(s: str, path: str, text: str = "f") -> dict[str, Any]:
    return {
        "commit_sha": s,
        "path": path,
        "llm_description": text,
        "llm_description_model": "o:m",
    }


def _batched_members() -> dict[str, Any]:
    """Cache members whose in-bundle repeats land in a later batch of 2."""
    return _members(
        **{
            "rationale.jsonl": [
                _card(line_start=1, line_end=2),
                _card(line_start=3, line_end=4),
                _card(line_start=5, line_end=6),
                _card(line_start=1, line_end=2, why="repeat"),  # batch 2
                _card(line_start=7, line_end=8, model="gpt-x"),
            ],
            "commit_descriptions.jsonl": [
                _desc(sha(10)),
                _desc(sha(11)),
                _desc(sha(12)),
                _desc(sha(10), "repeat"),  # batch 2
                _desc(sha(99)),  # no such commit
            ],
            "file_descriptions.jsonl": [
                _file_desc(sha(10), "a.py"),
                _file_desc(sha(10), "b.py"),
                _file_desc(sha(11), "a.py"),
                _file_desc(sha(10), "a.py", "repeat"),  # batch 2
                _file_desc(sha(11), "zzz.py"),  # no such file
            ],
        }
    )


def test_duplicates_across_a_batch_boundary_keep_dry_run_parity(
    target, tmp_path, monkeypatch
):
    from whygraph.portal import transfer_import

    monkeypatch.setattr(transfer_import, "CACHE_BATCH_ROWS", 2)
    _many_commits(target, [sha(10), sha(11), sha(12)], ["a.py", "b.py"])
    path = _bundle(tmp_path / "b.zip", _batched_members())
    before = _dump_hash(target.db)
    dry = _import(target, path, dry_run=True)
    assert _dump_hash(target.db) == before
    applied = _import(target, path)
    assert _counts_only(applied) == _counts_only(dry)
    assert applied["rationale"] == {
        "inserted": 4,
        "existing": 1,
        "inactive_model": 5,  # the target's default config is not openai
        "skipped_oversize": 0,
        "skipped_denied": 0,
    }
    assert applied["descriptions"] == {
        "filled": 6,
        "kept_target": 2,
        "missing_commit": 2,
        "skipped_oversize": 0,
    }
    with use_project(target.ctx), get_session() as session:
        from whygraph.db.models import ProjectImport

        log = session.exec(select(ProjectImport)).one()
        logged = json.loads(log.counts)
        assert logged["rationale"] == applied["rationale"]
        assert logged["descriptions"] == applied["descriptions"]
        first = session.exec(
            select(RationaleCache).where(RationaleCache.line_start == 1)
        ).one()
        assert first.why == "w"  # the first record of a key wins
        assert {r.import_id for r in session.exec(select(RationaleCache))} == {log.id}
        assert session.get(Commit, sha(10)).llm_description == "d"


def test_a_failed_cache_batch_keeps_earlier_batches_and_is_reported(
    target, tmp_path, monkeypatch
):
    import sqlite3

    from whygraph.db.models import ProjectImport
    from whygraph.mcp.rationale_cache import RationaleImport
    from whygraph.portal import transfer_import

    monkeypatch.setattr(transfer_import, "CACHE_BATCH_ROWS", 2)
    _many_commits(target, [sha(10), sha(11), sha(12)], ["a.py", "b.py"])
    real = RationaleImport.insert
    calls = []

    def locked_on_the_second(self, session, rows, *, import_id):
        calls.append(1)
        real(self, session, rows, import_id=import_id)  # counts, then fails
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(RationaleImport, "insert", locked_on_the_second)
    path = _bundle(tmp_path / "b.zip", _batched_members())
    result = _import(target, path)
    assert result["applied"] is True
    assert result["notes"]["inserted"]["confirmed"] == 3  # the core committed
    assert result["rationale"]["partial"] is True
    assert result["rationale"]["inserted"] == 2  # batch 1 only
    assert result["rationale"]["error"] == transfer_import.PARTIAL_ERROR
    assert "locked" not in result["rationale"]["error"]
    # The import stops: no description batch ran.
    assert result["descriptions"]["partial"] is True
    assert result["descriptions"]["filled"] == 0
    with use_project(target.ctx), get_session() as session:
        assert len(session.exec(select(RationaleCache)).all()) == 2
        assert session.get(Commit, sha(10)).llm_description is None
        logged = json.loads(session.exec(select(ProjectImport)).one().counts)
        assert logged["rationale"] == result["rationale"]
        assert logged["descriptions"] == result["descriptions"]

    # Importing again adds the rest.
    monkeypatch.setattr(RationaleImport, "insert", real)
    again = _import(target, path)
    assert "partial" not in again["rationale"]
    assert again["rationale"]["inserted"] == 2
    assert again["descriptions"]["filled"] == 6


def test_a_cache_format_error_writes_no_backup(target, tmp_path, monkeypatch):
    from whygraph.portal import transfer_import
    from whygraph.portal.transfer_bundle import BundleError

    monkeypatch.setattr(transfer_import, "CACHE_BATCH_ROWS", 2)
    files = [_file_desc(sha(1), f"f{i}.py") for i in range(5)]
    files.append(_file_desc(sha(1), "bad.py") | {"llm_description_model": "x"})
    path = _bundle(tmp_path / "b.zip", _members(**{"file_descriptions.jsonl": files}))
    before = _dump_hash(target.db)
    with pytest.raises(BundleError) as exc:
        _import(target, path)
    assert "file_descriptions.jsonl line 6" in exc.value.args[0]
    assert _dump_hash(target.db) == before
    assert not (target.db.parent / "backups").exists()


# --- settings: the portal's Postgres layer (local portal) ---


@pytest.fixture
def portal_target(env, tmp_path):  # noqa: F811 -- `env` is the imported fixture
    """A local portal with an initialized, scanned project ``demo``."""
    from test_portal_app import make_repo, portal_client
    from test_portal_mcp import _setup_project
    from whygraph.portal import db as portal_db
    from whygraph.portal.authz import ProjectRole
    from whygraph.portal.context import build_project_context
    from whygraph.portal.deps import bound_from
    from whygraph.portal.models import Project, User

    root = make_repo(env.shared, "demo")
    with portal_client() as client:
        _setup_project(client, root)
        with portal_db.get_session() as session:
            row = session.exec(select(Project)).one()
            row.last_scanned_head = OTHER_SHA
            session.add(row)
            session.flush()
            ctx = build_project_context(session, row)
            bound = bound_from(row, ctx, ProjectRole.ADMIN)
            uid = session.exec(select(User)).one().uid
            snapshot = SimpleNamespace(
                id=row.id,
                last_scanned_head=row.last_scanned_head,
                remote_url=row.remote_url,
                source=row.source,
                root=row.root,
            )
        with use_project(ctx), get_session() as session:
            session.connection().execute(
                insert(Commit),
                [
                    _commit_row(ROOT_SHA),
                    {**_commit_row(OTHER_SHA), "parent_shas": ROOT_SHA},
                ],
            )
        yield SimpleNamespace(
            client=client,
            state=client.app.state.portal,
            row=snapshot,
            bound=bound,
            ctx=ctx,
            db=bound.db_path,
            uid=uid,
        )


def _portal_import(t: SimpleNamespace, path: Path, **kw: Any) -> dict[str, Any]:
    from whygraph.portal import transfer_bundle as tb
    from whygraph.portal.transfer_import import apply_import

    options: dict[str, Any] = {
        "uid": t.uid,
        "keep_status": True,
        "apply_config": False,
        "dry_run": False,
        "mode": t.state.mode,
        "state": t.state,
    }
    options.update(kw)
    with tb.read_bundle(path) as bundle:
        return apply_import(t.row, t.bound, bundle, **options)


def _project_layer(t: SimpleNamespace) -> dict[str, Any]:
    from whygraph.portal import db as portal_db
    from whygraph.portal.config_layers import load_layer

    with portal_db.get_session() as session:
        return load_layer(session, t.bound.id, org_id=t.bound.org_id)


SETTINGS = {
    "rationale": {"model": "gpt-x"},
    "areas": {"everything": {"paths": ["*"]}},
    "scan": {"hooks": False},
}
AREA_NOTE = _note(
    7,
    anchor_kind="area",
    area="everything",
    anchor_path=None,
    line_start=None,
    line_end=None,
    body="Every change needs a migration note.",
)


def test_settings_preview_then_apply(portal_target, tmp_path):
    t = portal_target
    path = _bundle(
        tmp_path / "b.zip",
        _members(**{"notes.jsonl": [AREA_NOTE], "config.json": SETTINGS}),
    )
    before = _project_layer(t)
    dry = _portal_import(t, path, dry_run=True)
    assert _project_layer(t) == before
    config = dry["config"]
    assert config["present"] and config["invalid"] == [] and config["error"] is None
    assert config["applied"] is False
    # D15: scan.* never travels; the diff names only what changes.
    assert config["changes"] == [
        {"key": "areas.everything.paths", "from": None, "to": ["*"]},
        {"key": "rationale.model", "from": None, "to": "gpt-x"},
    ]
    # Q5: the area resolves only under the bundle's settings, and is wide there.
    assert [(w["target"], w["unresolved_area"]) for w in dry["wide_confirmed"]] == [
        ("everything", False)
    ]

    applied = _portal_import(t, path, apply_config=True)
    assert applied["config"]["applied"] is True
    layer = _project_layer(t)
    assert layer["rationale"] == {"model": "gpt-x"}
    assert layer["areas"] == {"everything": {"paths": ["*"]}}
    assert "scan" not in layer or layer.get("scan") == before.get("scan")
    # The cached context was invalidated: the next one carries the new model.
    assert t.state.contexts.get(t.bound.id).config.rationale.model == "gpt-x"
    # The card key followed the post-merge config: openai/default is now inactive.
    assert applied["rationale"]["inactive_model"] == 1


def test_invalid_settings_are_reported_and_not_applied(portal_target, tmp_path):
    t = portal_target
    bad = {"areas": {"everything": {"paths": ["../up"]}}}
    path = _bundle(tmp_path / "b.zip", _members(**{"config.json": bad}))
    before = _project_layer(t)
    result = _portal_import(t, path, apply_config=True)
    assert [item["key"] for item in result["config"]["invalid"]] == ["areas.everything"]
    assert result["config"]["applied"] is False
    assert _project_layer(t) == before
    assert result["notes"]["inserted"]["confirmed"] == 3  # the data still landed


def test_a_failed_settings_save_keeps_the_data(portal_target, tmp_path, monkeypatch):
    from whygraph.core.config import ConfigError
    from whygraph.db.models import ProjectImport
    from whygraph.portal import transfer_import

    t = portal_target
    path = _bundle(tmp_path / "b.zip", _members(**{"config.json": SETTINGS}))
    real = transfer_import.build_project_context
    calls = []

    def flaky(session, row, **kw):
        calls.append(1)
        if len(calls) > 1:  # the preflight passes, the save after commit fails
            raise ConfigError("boom")
        return real(session, row, **kw)

    monkeypatch.setattr(transfer_import, "build_project_context", flaky)
    before = _project_layer(t)
    result = _portal_import(t, path, apply_config=True)
    assert result["applied"] is True
    assert result["config"]["applied"] is False
    assert result["config"]["error"] == "boom"
    assert _project_layer(t) == before
    with use_project(t.ctx), get_session() as session:
        assert len(session.exec(select(ProjectImport)).all()) == 1


def test_a_partial_cache_stage_still_applies_the_settings(
    portal_target, tmp_path, monkeypatch
):
    import sqlite3

    from whygraph.analyze.backfill import DescriptionImport

    def full_disk(self, session, batch):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(DescriptionImport, "fill_commits", full_disk)
    t = portal_target
    path = _bundle(
        tmp_path / "b.zip",
        _members(**{"config.json": {"rationale": {"model": "gpt-x"}}}),
    )
    result = _portal_import(t, path, apply_config=True)
    assert "partial" not in result["rationale"]
    assert result["rationale"]["inserted"] == 1
    assert result["descriptions"]["partial"] is True
    assert result["config"]["applied"] is True
    assert _project_layer(t)["rationale"]["model"] == "gpt-x"


@pytest.mark.parametrize(
    "bundle_config",
    [
        {"llm": None, "analyze": None, "rationale": {"model": "gpt-x"}},
        {"llm": {"openai": None}, "rationale": {"model": "gpt-x"}},
    ],
)
def test_a_null_llm_table_keeps_the_endpoint_and_the_key(
    portal_target, tmp_path, bundle_config
):
    from whygraph.portal import db as portal_db
    from whygraph.portal.config_layers import save_layer
    from whygraph.portal.secrets import LLM_API_KEY, put_secret, read_secret

    t = portal_target
    own = {
        "llm": {
            "openai": {"base_url": "https://proxy.corp/v1", "model": "o"},
        }
    }
    with portal_db.get_session() as session:
        save_layer(session, t.bound.id, own, org_id=t.bound.org_id)
        put_secret(
            session,
            kind=LLM_API_KEY,
            provider="openai",
            value="sk-secret-value",
            project_id=t.bound.id,
            org_id=t.bound.org_id,
        )
        session.commit()
    path = _bundle(tmp_path / "b.zip", _members(**{"config.json": bundle_config}))
    result = _portal_import(t, path, apply_config=True)
    assert result["config"]["applied"] is True, result["config"]
    layer = _project_layer(t)
    assert layer["llm"] == own["llm"]
    assert layer["rationale"] == {"model": "gpt-x"}
    with portal_db.get_session() as session:
        assert (
            read_secret(
                session,
                kind=LLM_API_KEY,
                provider="openai",
                project_id=t.bound.id,
                org_id=t.bound.org_id,
            )
            == "sk-secret-value"
        )
