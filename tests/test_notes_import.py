"""Importing a transfer bundle's notes (``whygraph.mcp.notes.import_notes``), plan 42 step 3.

Covers ``core/import_checks.py`` (``clean_text``, ``ImportFormatError``), the
lexical path check, every rule of plan 42 section 5.3's table (per-kind stored
columns, bodies, refs, origins, timestamps, uids, statuses, purpose notes,
supersede links), the bundle-level checks, provenance and duplicate dedupe,
and the call's transaction contract. Everything runs against a file-backed
SQLite DB; nothing touches the repository's files but the symlink test.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import insert
from sqlmodel import col, select

from whygraph.core.config import Config
from whygraph.core.context import ProjectContext, use_project
from whygraph.core import import_checks
from whygraph.core.import_checks import ImportFormatError, clean_text
from whygraph.db import ensure_initialized, get_session
from whygraph.db.models import Commit, ProjectNote, PullRequest
from whygraph.mcp import notes
from whygraph.mcp.errors import WhyGraphError

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
T0 = "2026-09-01T10:00:00+00:00"
T1 = "2026-09-02T10:00:00+00:00"
T2 = "2026-09-03T10:00:00+00:00"
SHA = "a" * 40
HASH = "b" * 64


@pytest.fixture
def project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SimpleNamespace]:
    """A repo and a migrated WhyGraph DB, bound as the current project."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    db = tmp_path / "whygraph.db"
    ctx = ProjectContext(slug="demo", root=root, config=Config(whygraph_db=db))
    monkeypatch.setattr(notes, "_utcnow", lambda: NOW)
    with use_project(ctx):
        ensure_initialized()
        yield SimpleNamespace(root=root, db=db, ctx=ctx)


def _rec(note_id: int, **kw: Any) -> dict[str, Any]:
    """A confirmed decision on the project anchor, in wire form."""
    base: dict[str, Any] = {
        "id": note_id,
        "kind": "decision",
        "body": f"Note {note_id}.",
        "edited_on_confirm": False,
        "status": "confirmed",
        "anchor_kind": "project",
        "anchor_path": None,
        "line_start": None,
        "line_end": None,
        "qualified_name": None,
        "area": None,
        "content_hash": None,
        "source": "human",
        "origin": "ui",
        "refs": None,
        "supersedes_id": None,
        "proposed_by": "src-u1",
        "proposed_at": T0,
        "confirmed_by": "src-u2",
        "confirmed_at": T1,
        "reconfirmed_by": None,
        "reconfirmed_at": None,
        "retired_by": None,
        "retired_at": None,
        "retire_reason": None,
    }
    return {**base, **kw}


def _proposed(note_id: int, **kw: Any) -> dict[str, Any]:
    values = {"status": "proposed", "confirmed_by": None, "confirmed_at": None}
    return _rec(note_id, **{**values, **kw})


def _retired(note_id: int, reason: str = "outdated", **kw: Any) -> dict[str, Any]:
    values = {"retired_by": "src-u2", "retired_at": T2, "retire_reason": reason}
    return _rec(note_id, status="retired", **{**values, **kw})


def _purpose(note_id: int, status: str = "confirmed", **kw: Any) -> dict[str, Any]:
    maker = _rec if status == "confirmed" else _proposed
    return maker(note_id, kind="purpose", source="analysis", **kw)


def _import(
    records: list[dict[str, Any]],
    *,
    keep_status: bool = True,
    chat_ids: dict[int, int] | None = None,
    purpose_allowed: bool = True,
    actor: str | None = "importer",
) -> notes.NotesImportResult:
    with get_session() as session:
        return notes.import_notes(
            session,
            records,
            keep_status=keep_status,
            actor=actor,
            chat_ids=chat_ids or {},
            purpose_allowed=purpose_allowed,
        )


def _format_error(records: list[dict[str, Any]], **kw: Any) -> ImportFormatError:
    with pytest.raises(ImportFormatError) as info:
        _import(records, **kw)
    return info.value


def _rows() -> list[ProjectNote]:
    with get_session() as session:
        rows = list(
            session.exec(select(ProjectNote).order_by(col(ProjectNote.id))).all()
        )
        for row in rows:
            session.expunge(row)
    return rows


def _target_note(**values: Any) -> int:
    defaults = {
        "kind": "decision",
        "body": "target",
        "status": "confirmed",
        "anchor_kind": "project",
        "source": "human",
        "proposed_by": "t-u1",
        "proposed_at": T0,
        "confirmed_by": "t-u2",
        "confirmed_at": T1,
    }
    with get_session() as session:
        row = ProjectNote(**{**defaults, **values})
        session.add(row)
        session.flush()
        return int(row.id or 0)


# ---------------------------------------------------------------------------
# core/import_checks.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "a\x00b",
        "a\tb",
        "a\nb",
        "a\x7fb",
        "a\x85b",
        "a‮b",
        "a⁦b",
        "a\u2028b",
        "a\u2029b",
        "a\u200eb",
        "a\u200fb",
        "a\u061cb",
        "a\ud800b",
        "a\udfffb",
    ],
)
def test_clean_text_single_line_refuses_controls_and_bidi(value: str) -> None:
    with pytest.raises(ImportFormatError) as info:
        clean_text(value, multiline=False, max_chars=100, field="title")
    assert info.value.field == "title"
    assert value not in str(info.value)


def test_clean_text_multi_line_replaces_controls() -> None:
    text = "a\tb\nc\rd\x00e\x07f\x7fg\x9fh‮i"
    assert clean_text(text, multiline=True, max_chars=100) == "a\tb\nc\rd�e�f�g�h‮i"
    assert clean_text("", multiline=True, max_chars=0) == ""


def test_clean_text_multi_line_replaces_lone_surrogates_only() -> None:
    text = "a\ud800b\udfffc\U0001f600d\u2028e\u200ff"
    assert clean_text(text, multiline=True, max_chars=100) == (
        "a\ufffdb\ufffdc\U0001f600d\u2028e\u200ff"
    )


def test_clean_text_fast_path_never_skips_a_refused_character() -> None:
    """The ``isprintable`` fast path and the regex slow path agree on every BMP point."""
    for code in range(0x10000):
        ch = chr(code)
        if not import_checks.SINGLE_LINE_BAD.match(ch):
            continue
        assert not ch.isprintable(), hex(code)
        with pytest.raises(ImportFormatError):
            clean_text(f"a{ch}b", multiline=False, max_chars=100)
        # One line break sends a multi-line text down the slow path.
        fast = clean_text(f"a{ch}b", multiline=True, max_chars=100)
        slow = clean_text(f"a{ch}b\n", multiline=True, max_chars=100)
        assert fast is not None and slow == fast + "\n", hex(code)


def test_clean_text_length_and_type() -> None:
    assert clean_text("abc", multiline=False, max_chars=3) == "abc"
    assert clean_text("abcd", multiline=False, max_chars=3) is None
    assert clean_text("abcd", multiline=True, max_chars=3) is None
    with pytest.raises(ImportFormatError):
        clean_text(3, multiline=True, max_chars=3)
    with pytest.raises(ImportFormatError):
        clean_text(None, multiline=False, max_chars=3)


def test_import_format_error_carries_field_reason_and_index() -> None:
    exc = ImportFormatError("body", "must be text", index=4)
    assert (exc.field, exc.reason, exc.index) == ("body", "must be text", 4)
    assert str(exc) == "body: must be text"


# ---------------------------------------------------------------------------
# The lexical path check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        None,
        3,
        "",
        "/etc/passwd",
        "a\\b",
        "a\x00b",
        "a\nb",
        "a‮b",
        "../x",
        "a/../b",
        "./a",
        "a/./b",
        "a//b",
        "src/",
        ".",
        "x" * 4097,
    ],
)
def test_lexical_path_refuses(path: object) -> None:
    with pytest.raises(ImportFormatError):
        notes._lexical_path(path)


@pytest.mark.parametrize(
    "path",
    [".env", ".ENV", ".Env.local", ".git/config", ".Git/config", "a/.WHYGRAPH/x"],
)
def test_lexical_path_denies_in_any_case(path: str) -> None:
    assert notes._lexical_path(path) is None


def test_lexical_path_returns_the_path_unchanged() -> None:
    assert notes._lexical_path("src/Pkg/a.py") == "src/Pkg/a.py"
    assert notes._lexical_path("does/not/exist.py") == "does/not/exist.py"


def test_a_symlinked_target_path_is_accepted(project: SimpleNamespace) -> None:
    (project.root / "link").symlink_to(project.root / "src")
    result = _import([_rec(1, anchor_kind="path", anchor_path="link/a.py")])
    assert result.inserted["confirmed"] == 1
    assert _rows()[0].anchor_path == "link/a.py"


# ---------------------------------------------------------------------------
# Per-kind stored columns
# ---------------------------------------------------------------------------


def test_every_anchor_kind_stores_its_columns(project: SimpleNamespace) -> None:
    records = [
        _rec(1),
        _rec(2, anchor_kind="area", area="chat-ui"),
        _rec(3, anchor_kind="path", anchor_path="src"),
        _rec(
            4,
            anchor_kind="symbol",
            qualified_name="pkg.a",
            anchor_path="src/a.py",
            line_start=1,
            line_end=2,
            content_hash=HASH,
        ),
        _rec(
            5,
            anchor_kind="range",
            anchor_path="src/a.py",
            line_start=3,
            line_end=3,
            content_hash=None,
        ),
    ]
    result = _import(records)
    assert result.inserted == {"proposed": 0, "confirmed": 5, "retired": 0}
    rows = _rows()
    assert [r.anchor_kind for r in rows] == [
        "project",
        "area",
        "path",
        "symbol",
        "range",
    ]
    assert rows[1].area == "chat-ui"
    assert (rows[3].qualified_name, rows[3].anchor_path, rows[3].content_hash) == (
        "pkg.a",
        "src/a.py",
        HASH,
    )
    assert (rows[4].line_start, rows[4].line_end, rows[4].content_hash) == (3, 3, None)


@pytest.mark.parametrize(
    "record",
    [
        _rec(1, anchor_path="src"),
        _rec(1, anchor_kind="area", area="chat", anchor_path="src"),
        _rec(1, anchor_kind="path", anchor_path="src", line_start=1, line_end=1),
        _rec(1, anchor_kind="path", anchor_path="src", qualified_name="x"),
        _rec(1, anchor_kind="range", anchor_path="a", line_start=1, line_end=1, area="x"),
        _rec(1, anchor_kind="area", area="Chat"),
        _rec(1, anchor_kind="area", area=None),
        _rec(1, anchor_kind="path", anchor_path=None),
        _rec(1, anchor_kind="range", anchor_path="a", line_start=0, line_end=1),
        _rec(1, anchor_kind="range", anchor_path="a", line_start=3, line_end=2),
        _rec(1, anchor_kind="range", anchor_path="a", line_start=1, line_end=None),
        _rec(1, anchor_kind="range", anchor_path="a", line_start=True, line_end=1),
        _rec(1, anchor_kind="range", anchor_path="a", line_start=1, line_end=1,
             content_hash="B" * 64),
        _rec(1, anchor_kind="symbol", qualified_name="  ", anchor_path="a",
             line_start=1, line_end=1),
        _rec(1, anchor_kind="symbol", qualified_name="a‮b", anchor_path="a",
             line_start=1, line_end=1),
        _rec(1, anchor_kind="symbol", qualified_name=None, anchor_path="a",
             line_start=1, line_end=1),
    ],
)  # fmt: skip
def test_anchor_format_violations_refuse(
    project: SimpleNamespace, record: dict[str, Any]
) -> None:
    _format_error([record])
    assert _rows() == []


def test_anchor_size_violations_skip(project: SimpleNamespace) -> None:
    records = [
        _rec(
            1, anchor_kind="range", anchor_path="a", line_start=1, line_end=10_000_001
        ),
        _rec(
            2,
            anchor_kind="symbol",
            qualified_name="q" * 1_001,
            anchor_path="a",
            line_start=1,
            line_end=1,
        ),
        _rec(
            3, anchor_kind="range", anchor_path="a", line_start=1, line_end=10_000_000
        ),
    ]
    result = _import(records)
    assert result.skipped_oversize == 2
    assert result.inserted["confirmed"] == 1


def test_deny_listed_paths_are_skipped_and_counted(project: SimpleNamespace) -> None:
    records = [
        _rec(1, anchor_kind="path", anchor_path=".ENV"),
        _rec(2, anchor_kind="path", anchor_path=".Git/config"),
        _rec(
            3,
            anchor_kind="symbol",
            qualified_name="secret",
            anchor_path=".whygraph/x.py",
            line_start=1,
            line_end=1,
        ),
        _rec(4, anchor_kind="path", anchor_path="src"),
    ]
    result = _import(records)
    assert result.skipped_denied == 3
    assert [r.anchor_path for r in _rows()] == ["src"]
    assert set(result.id_map) == {4}


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["a\tb", "a\rb", "a\x7fb", "a\x00b", "   ", None, 3])
def test_body_follows_the_clean_body_rules(
    project: SimpleNamespace, body: object
) -> None:
    exc = _format_error([_rec(1, body=body)])
    assert exc.field == "body"


def test_a_lone_surrogate_in_a_body_refuses(project: SimpleNamespace) -> None:
    exc = _format_error([_rec(1, body="half \ud800 a pair")])
    assert exc.field == "body"
    assert "\ud800" not in str(exc)


def test_body_is_trimmed_and_keeps_newlines(project: SimpleNamespace) -> None:
    _import([_rec(1, body="  line one\nline two  ")])
    assert _rows()[0].body == "line one\nline two"


def test_an_over_long_body_is_skipped(project: SimpleNamespace) -> None:
    result = _import([_rec(1, body="x" * 1_001), _rec(2, body="y" * 1_000)])
    assert result.skipped_oversize == 1
    assert result.inserted["confirmed"] == 1


def test_purpose_cap_depends_on_the_stored_status(project: SimpleNamespace) -> None:
    body = "p" * 900
    assert _import([_purpose(1, body=body)]).inserted["confirmed"] == 1
    with get_session() as session:
        session.exec(  # type: ignore[call-overload]
            ProjectNote.__table__.delete()
        )
    assert _import([_purpose(1, "proposed", body=body)]).skipped_oversize == 1
    downgraded = _import([_purpose(1, body=body)], keep_status=False)
    assert downgraded.skipped_oversize == 1
    assert _rows() == []


def test_edited_on_confirm_stores_an_empty_proposed_body(
    project: SimpleNamespace,
) -> None:
    _import(
        [
            _rec(1, edited_on_confirm=True),
            _rec(2, edited_on_confirm=False),
            _proposed(3, edited_on_confirm=True),
            _retired(4, edited_on_confirm=True),
        ]
    )
    assert [r.proposed_body for r in _rows()] == ["", None, None, ""]


# ---------------------------------------------------------------------------
# Enums, refs, origins, timestamps, uids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record", "field"),
    [
        (_rec(1, kind="idea"), "kind"),
        (_rec(1, anchor_kind="file"), "anchor_kind"),
        (_rec(1, source="bot"), "source"),
        (_rec(1, status="open"), "status"),
        (_retired(1, reason="bored"), "retire_reason"),
        (_rec(1, source="analysis"), "source"),
        (_rec(1, kind="purpose", source="human"), "kind"),
        (_rec(1, kind="purpose", source="analysis", anchor_kind="path",
              anchor_path="src"), "kind"),
        (_rec(1, id=-1), "id"),
        (_rec(1, id="1"), "id"),
        (_rec(1, id=2**31 + 1), "id"),
        (_rec(1, edited_on_confirm="yes"), "edited_on_confirm"),
        (_rec(1, origin="chat:x"), "origin"),
        (_rec(1, origin="somewhere"), "origin"),
    ],
)  # fmt: skip
def test_value_set_violations_refuse(
    project: SimpleNamespace, record: dict[str, Any], field: str
) -> None:
    assert _format_error([record]).field == field


@pytest.mark.parametrize(
    "refs",
    [
        "abc",
        [SHA] * 6,
        [SHA.upper()],
        [SHA[:39]],
        ["#0"],
        ["#1234567890"],
        ["#-1"],
        [3],
    ],
)
def test_ref_format_violations_refuse(project: SimpleNamespace, refs: object) -> None:
    assert _format_error([_rec(1, refs=refs)]).field == "refs"


def test_refs_are_kept_verbatim_and_unresolved_counted(
    project: SimpleNamespace,
) -> None:
    with get_session() as session:
        session.add(
            Commit(
                sha=SHA,
                parent_shas="",
                author_name="T",
                author_email="t@example.com",
                authored_at=T0,
                committed_at=T0,
                subject="s",
                body="",
                files_changed=0,
                insertions=0,
                deletions=0,
                scanned_at=T0,
                on_default_branch=1,
            )
        )
        session.add(
            PullRequest(
                number=7,
                title="t",
                state="open",
                created_at="x",
                updated_at="x",
                head_sha="h",
                base_ref="main",
                html_url="u",
                labels="[]",
                fetched_at="x",
            )
        )
    refs = [SHA, "c" * 40, "#7", "#8", SHA]
    result = _import([_rec(1, refs=refs), _rec(2, refs=[])])
    assert result.refs_unresolved == 2
    rows = _rows()
    assert json.loads(rows[0].refs or "") == [SHA, "c" * 40, "#7", "#8"]
    assert rows[1].refs is None


def test_chat_origins_are_remapped_or_nulled(project: SimpleNamespace) -> None:
    records = [
        _rec(1, origin="chat:5"),
        _rec(2, origin="chat:6"),
        _rec(3, origin="mcp"),
        _rec(4, origin=None),
    ]
    _import(records, chat_ids={5: 41})
    assert [r.origin for r in _rows()] == ["chat:41", None, "mcp", None]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proposed_at", "yesterday"),
        ("proposed_at", 12),
        ("proposed_at", None),
        ("confirmed_at", "2026-10-30T12:06:00+00:00"),
        ("confirmed_at", "２０２６-01-01"),
        ("proposed_at", "2026-10-30T12:06:00"),
    ],
)
def test_timestamp_violations_refuse(
    project: SimpleNamespace, field: str, value: object
) -> None:
    _format_error([_rec(1, **{field: value})])


def test_timestamps_within_the_slack_are_kept_verbatim(
    project: SimpleNamespace,
) -> None:
    soon = (NOW + timedelta(minutes=4)).isoformat()
    _import([_rec(1, proposed_at="2026-09-01T10:00:00Z", confirmed_at=soon)])
    row = _rows()[0]
    assert (row.proposed_at, row.confirmed_at) == ("2026-09-01T10:00:00Z", soon)


def test_uids_are_stored_namespaced(project: SimpleNamespace) -> None:
    _import(
        [
            _rec(1, reconfirmed_by="src-u3", reconfirmed_at=T2),
            _retired(2, confirmed_by="src-u2", confirmed_at=T1),
        ]
    )
    first, second = _rows()
    assert (first.proposed_by, first.confirmed_by, first.reconfirmed_by) == (
        "imported:src-u1",
        "imported:src-u2",
        "imported:src-u3",
    )
    assert second.retired_by == "imported:src-u2"


@pytest.mark.parametrize(
    "record",
    [
        _rec(1, proposed_by="imported:src-u1"),
        _rec(1, confirmed_by="imported:x"),
        _rec(1, proposed_by=""),
        _rec(1, confirmed_by=""),
        _rec(1, proposed_by=None),
        _rec(1, proposed_by="u\nx"),
        _rec(1, proposed_by="u" * 201),
    ],
)
def test_uid_violations_refuse(
    project: SimpleNamespace, record: dict[str, Any]
) -> None:
    _format_error([record])


@pytest.mark.parametrize(
    "record",
    [
        _rec(1, confirmed_by=None, confirmed_at=None),
        _rec(1, confirmed_at=None),
        _proposed(1, confirmed_by="x", confirmed_at=T1),
        _rec(1, retired_by="x", retired_at=T2, retire_reason="outdated"),
        _retired(1, retired_by=None, retired_at=None),
        _retired(1, reason=None),
        _proposed(1, reconfirmed_by="x", reconfirmed_at=T2),
        _rec(1, reconfirmed_by="x"),
    ],
)
def test_lifecycle_fields_must_match_the_status(
    project: SimpleNamespace, record: dict[str, Any]
) -> None:
    _format_error([record])


# ---------------------------------------------------------------------------
# Bundle-level checks and the error index
# ---------------------------------------------------------------------------


def test_two_live_purposes_of_one_status_refuse(project: SimpleNamespace) -> None:
    exc = _format_error([_purpose(1), _purpose(2, body="other")])
    assert (exc.field, exc.index) == ("kind", 1)
    exc = _format_error([_purpose(1, "proposed"), _purpose(2, "proposed", body="b")])
    assert exc.index == 1


def test_retired_purposes_do_not_count_toward_the_purpose_rule(
    project: SimpleNamespace,
) -> None:
    records = [
        _purpose(1, status="confirmed", body="old"),
        _purpose(2, "proposed", body="rejected one"),
        _purpose(3, status="confirmed", body="new"),
    ]
    records[0] |= {"status": "retired", "retired_by": "u", "retired_at": T2,
                   "retire_reason": "superseded"}  # fmt: skip
    records[1] |= {"status": "retired", "retired_by": "u", "retired_at": T2,
                   "retire_reason": "superseded"}  # fmt: skip
    assert _import(records).inserted == {"proposed": 0, "confirmed": 1, "retired": 2}


def test_supersedes_must_name_an_earlier_note_of_the_bundle(
    project: SimpleNamespace,
) -> None:
    exc = _format_error([_rec(1, supersedes_id=2), _rec(2)])
    assert (exc.field, exc.index) == ("supersedes_id", 0)
    assert _format_error([_rec(1, supersedes_id=1)]).field == "supersedes_id"
    assert _format_error([_rec(2, supersedes_id=1)]).field == "supersedes_id"
    assert _format_error([_rec(2, supersedes_id="1")]).field == "supersedes_id"


def test_a_repeated_id_refuses(project: SimpleNamespace) -> None:
    exc = _format_error([_rec(1), _rec(1, body="again")])
    assert (exc.field, exc.index) == ("id", 1)


def test_a_format_error_names_its_record_and_writes_nothing(
    project: SimpleNamespace,
) -> None:
    exc = _format_error([_rec(1), _rec(2), _rec(3, body="a\tsecret")])
    assert exc.index == 2
    assert "secret" not in str(exc)
    assert _rows() == []


def test_records_go_in_by_source_id(project: SimpleNamespace) -> None:
    result = _import([_rec(9), _rec(3), _proposed(5, supersedes_id=3)])
    rows = _rows()
    assert [r.body for r in rows] == ["Note 3.", "Note 5.", "Note 9."]
    assert result.id_map == {3: rows[0].id, 5: rows[1].id, 9: rows[2].id}
    assert rows[1].supersedes_id == rows[0].id


# ---------------------------------------------------------------------------
# Supersede links
# ---------------------------------------------------------------------------


def test_a_supersede_chain_links(project: SimpleNamespace) -> None:
    records = [
        _retired(1, reason="superseded"),
        _rec(2, supersedes_id=1),
        _proposed(3, supersedes_id=2, body="revision"),
    ]
    result = _import(records)
    old, current, revision = _rows()
    assert current.supersedes_id == old.id
    assert revision.supersedes_id == current.id
    assert result.revision_unlinked == 0


def test_a_supersede_to_a_skipped_note_is_unlinked(project: SimpleNamespace) -> None:
    records = [
        _rec(1, anchor_kind="path", anchor_path=".env"),
        _rec(2, body="x" * 1_001),
        _proposed(3, supersedes_id=1),
        _proposed(4, supersedes_id=2, body="other"),
    ]
    result = _import(records)
    assert result.revision_unlinked == 2
    assert [r.supersedes_id for r in _rows()] == [None, None]


def test_a_supersede_through_a_dedupe_hit_to_a_proposed_note_is_unlinked(
    project: SimpleNamespace,
) -> None:
    target = _target_note(
        body="Same text.", status="proposed", confirmed_by=None, confirmed_at=None
    )
    result = _import([_rec(1, body="Same  text."), _proposed(2, supersedes_id=1)])
    assert result.id_map[1] == target
    assert result.skipped_duplicate == 1
    assert result.revision_unlinked == 1
    assert _rows()[-1].supersedes_id is None


def test_a_supersede_through_a_dedupe_hit_to_a_confirmed_note_links(
    project: SimpleNamespace,
) -> None:
    target = _target_note(body="Same text.")
    result = _import([_rec(1, body="Same text."), _proposed(2, supersedes_id=1)])
    assert result.revision_unlinked == 0
    assert _rows()[-1].supersedes_id == target


# ---------------------------------------------------------------------------
# Dedupe and idempotence
# ---------------------------------------------------------------------------


def test_provenance_dedupe_hits_a_retired_target_note(project: SimpleNamespace) -> None:
    retired = _target_note(
        body="Gone now.",
        proposed_by="imported:src-u1",
        status="retired",
        retired_by="t-u2",
        retired_at=T2,
        retire_reason="wrong",
    )
    result = _import([_rec(1, body="Gone   now.")])
    assert result.id_map == {1: retired}
    assert result.inserted_ids == {}
    assert result.skipped_duplicate == 1
    assert len(_rows()) == 1


def test_provenance_matches_an_unprefixed_target_uid(project: SimpleNamespace) -> None:
    """A -> B -> A: the note came home, and its author is a real member here."""
    home = _target_note(
        body="Round trip.",
        proposed_by="src-u1",
        status="retired",
        retired_by="t",
        retired_at=T2,
        retire_reason="outdated",
    )
    assert _import([_rec(1, body="Round trip.")]).id_map == {1: home}


def test_provenance_needs_the_same_time_and_author(project: SimpleNamespace) -> None:
    _target_note(
        body="Gone now.",
        proposed_by="imported:other",
        status="retired",
        retired_by="t",
        retired_at=T2,
        retire_reason="wrong",
    )
    assert _import([_rec(1, body="Gone now.")]).inserted["confirmed"] == 1


def test_a_retired_record_is_not_a_duplicate_of_a_live_note(
    project: SimpleNamespace,
) -> None:
    _target_note(body="Text.")
    result = _import([_retired(1, body="Text.")])
    assert result.inserted["retired"] == 1


def test_duplicate_dedupe_ignores_the_note_kind_like_propose(
    project: SimpleNamespace,
) -> None:
    target = _target_note(
        kind="gotcha", body="Same.", anchor_kind="path", anchor_path="src"
    )
    result = _import([_rec(1, anchor_kind="path", anchor_path="src", body="Same.")])
    assert result.id_map == {1: target}


def test_dedupe_inside_one_bundle(project: SimpleNamespace) -> None:
    result = _import([_rec(1, body="Twice."), _proposed(2, body="Twice.")])
    assert result.skipped_duplicate == 1
    assert result.id_map[2] == result.id_map[1]


def test_a_re_import_adds_nothing(project: SimpleNamespace) -> None:
    records = [
        _retired(1, reason="superseded"),
        _rec(2, supersedes_id=1, origin="chat:3"),
        _proposed(3, supersedes_id=2, body="revision"),
        _purpose(4),
        _rec(5, anchor_kind="range", anchor_path="src/a.py", line_start=1, line_end=1),
    ]
    first = _import(records, chat_ids={3: 30})
    before = [(r.id, r.body, r.status, r.supersedes_id) for r in _rows()]
    second = _import(records, chat_ids={3: 30}, purpose_allowed=True)
    assert second.inserted == {"proposed": 0, "confirmed": 0, "retired": 0}
    assert second.skipped_duplicate == 5
    assert second.id_map == first.id_map
    assert second.inserted_ids == {}
    assert [(r.id, r.body, r.status, r.supersedes_id) for r in _rows()] == before


def test_the_dedupe_index_handles_many_rows(project: SimpleNamespace) -> None:
    with get_session() as session:
        session.exec(  # type: ignore[call-overload]
            insert(ProjectNote),
            params=[
                {
                    "kind": "decision",
                    "body": f"target {n}",
                    "status": "confirmed",
                    "anchor_kind": "path",
                    "anchor_path": "src",
                    "source": "human",
                    "proposed_by": "t",
                    "proposed_at": T0,
                    "confirmed_by": "t",
                    "confirmed_at": T1,
                }
                for n in range(5_000)
            ],
        )
    records = [
        _rec(n, anchor_kind="path", anchor_path="src", body=f"bundle {n}")
        for n in range(1, 5_001)
    ]
    result = _import(records)
    assert result.inserted["confirmed"] == 5_000


# ---------------------------------------------------------------------------
# Status and purpose
# ---------------------------------------------------------------------------


def test_keep_status_keeps_confirmed_and_returns_their_ids(
    project: SimpleNamespace,
) -> None:
    result = _import([_rec(1), _proposed(2), _retired(3), _rec(4)])
    assert result.inserted == {"proposed": 1, "confirmed": 2, "retired": 1}
    assert result.confirmed_ids == [result.id_map[1], result.id_map[4]]
    assert result.downgraded == 0


def test_without_keep_status_confirmed_becomes_proposed(
    project: SimpleNamespace,
) -> None:
    result = _import(
        [
            _rec(1, edited_on_confirm=True, reconfirmed_by="r", reconfirmed_at=T2),
            _retired(2),
        ],
        keep_status=False,
    )
    assert result.inserted == {"proposed": 1, "confirmed": 0, "retired": 1}
    assert result.downgraded == 1
    assert result.confirmed_ids == []
    first, second = _rows()
    assert (first.status, first.confirmed_by, first.confirmed_at) == (
        "proposed",
        None,
        None,
    )
    assert (first.reconfirmed_by, first.reconfirmed_at, first.proposed_body) == (
        None,
        None,
        None,
    )
    assert (second.status, second.confirmed_by) == ("retired", "imported:src-u2")


def test_a_confirmed_purpose_and_its_pending_revision_both_import(
    project: SimpleNamespace,
) -> None:
    records = [_purpose(1), _purpose(2, "proposed", body="Revised.", supersedes_id=1)]
    result = _import(records)
    assert result.inserted == {"proposed": 1, "confirmed": 1, "retired": 0}
    assert result.revision_unlinked == 0
    confirmed, proposed = _rows()
    assert proposed.supersedes_id == confirmed.id
    assert notes.purpose_notes()["confirmed"]["id"] == confirmed.id  # type: ignore[index]


def _revised_since_last_import(original: dict[str, Any]) -> list[dict[str, Any]]:
    """The source after confirming a revision of ``original``: retired, then the revision."""
    retired = {
        **original,
        "status": "retired",
        "retired_by": "src-u2",
        "retired_at": T2,
        "retire_reason": "superseded",
    }
    revision = {
        **original,
        "id": 2,
        "body": "Revised.",
        "supersedes_id": 1,
        "proposed_at": T1,
        "confirmed_at": T2,
    }
    return [retired, revision]


def test_a_confirmed_revision_of_a_confirmed_target_note_imports_proposed(
    project: SimpleNamespace,
) -> None:
    """Never two confirmed versions: the target's note wins until an admin confirms."""
    original = _rec(1, body="Original.", edited_on_confirm=True)
    first = _import([original])
    target = first.id_map[1]
    result = _import(
        _revised_since_last_import(
            {**original, "reconfirmed_by": "src-u3", "reconfirmed_at": T2}
        )
    )
    assert result.id_map[1] == target
    assert result.inserted == {"proposed": 1, "confirmed": 0, "retired": 0}
    assert result.downgraded == 1
    assert result.revision_unlinked == 0
    assert result.confirmed_ids == []
    old, revision = _rows()
    assert (old.id, old.status) == (target, "confirmed")
    assert (revision.status, revision.supersedes_id) == ("proposed", target)
    assert (revision.confirmed_by, revision.confirmed_at) == (None, None)
    assert (revision.reconfirmed_by, revision.reconfirmed_at) == (None, None)
    assert revision.proposed_body is None
    # The normal flow then retires the target's version natively.
    notes.confirm(revision.id, actor_uid="admin", actor_can_configure=True)
    old, revision = _rows()
    assert (old.status, old.retire_reason) == ("retired", "superseded")
    assert revision.status == "confirmed"


def test_a_confirmed_purpose_revision_never_leaves_two_live_purposes(
    project: SimpleNamespace,
) -> None:
    first = _import([_purpose(1, body="Original.")])
    target = first.id_map[1]
    records = _revised_since_last_import(_purpose(1, body="Original."))
    # The caller's D11 decision: the target already has a live purpose.
    result = _import(records, purpose_allowed=False)
    assert result.skipped_purpose == 2
    # Even when called as if it had none, the revision never goes in.
    result = _import(records, purpose_allowed=True)
    assert result.id_map[1] == target
    assert result.skipped_purpose == 1
    assert result.inserted == {"proposed": 0, "confirmed": 0, "retired": 0}
    (row,) = _rows()
    assert (row.id, row.status) == (target, "confirmed")


def test_purpose_notes_are_skipped_when_not_allowed(project: SimpleNamespace) -> None:
    records = [
        _purpose(1),
        _purpose(2, "proposed", body="Revised.", supersedes_id=1),
        _rec(3),
    ]
    result = _import(records, purpose_allowed=False)
    assert result.skipped_purpose == 2
    assert result.inserted["confirmed"] == 1
    assert set(result.id_map) == {3}


def test_without_keep_status_only_the_pending_purpose_imports(
    project: SimpleNamespace,
) -> None:
    records = [_purpose(1), _purpose(2, "proposed", body="Revised.", supersedes_id=1)]
    result = _import(records, keep_status=False)
    assert result.skipped_purpose == 1
    assert result.revision_unlinked == 1
    (row,) = _rows()
    assert (row.body, row.status, row.supersedes_id) == ("Revised.", "proposed", None)


# ---------------------------------------------------------------------------
# The call's contract
# ---------------------------------------------------------------------------


def test_it_never_commits(project: SimpleNamespace) -> None:
    with get_session() as session:
        result = notes.import_notes(
            session,
            [_rec(1), _proposed(2)],
            keep_status=True,
            actor="importer",
            chat_ids={},
            purpose_allowed=True,
        )
        assert sum(result.inserted.values()) == 2
        session.rollback()
    assert _rows() == []


def test_counts_shape(project: SimpleNamespace) -> None:
    counts = _import([_rec(1)]).counts()
    assert counts == {
        "inserted": {"proposed": 0, "confirmed": 1, "retired": 0},
        "skipped_duplicate": 0,
        "skipped_purpose": 0,
        "skipped_oversize": 0,
        "skipped_denied": 0,
        "refs_unresolved": 0,
        "revision_unlinked": 0,
        "downgraded": 0,
    }


def test_wire_models_with_model_dump_are_accepted(project: SimpleNamespace) -> None:
    class Wire:
        def __init__(self, values: dict[str, Any]) -> None:
            self.values = values

        def model_dump(self) -> dict[str, Any]:
            return dict(self.values)

    result = _import([Wire(_rec(1))])  # type: ignore[list-item]
    assert result.inserted["confirmed"] == 1


def test_no_actor_and_linked_project(project: SimpleNamespace) -> None:
    with pytest.raises(WhyGraphError) as info:
        _import([_rec(1)], actor=None)
    assert info.value.code == "note_no_actor"
    linked = ProjectContext(
        slug="demo",
        root=project.root,
        config=project.ctx.config,
        remote=object(),  # type: ignore[arg-type]
    )
    with use_project(linked):
        with pytest.raises(WhyGraphError) as info:
            notes.import_notes(
                None,  # type: ignore[arg-type]
                [_rec(1)],
                keep_status=True,
                actor="importer",
                chat_ids={},
                purpose_allowed=True,
            )
    assert info.value.code == "note_linked"
