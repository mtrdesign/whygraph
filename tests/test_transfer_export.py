"""Project transfer export (``whygraph.portal.transfer_export``), plan 42 step 7.

Runs against a file-backed WhyGraph DB bound as the current project; the two
portal-DB reads (the config layer, user names) are replaced, so no Postgres is
needed. Covers the D13 note filter, ``edited_on_confirm``, uid prefix handling
and the ``imported_actor``-only name lookup, ``origin_key``, the D15 config
filter and the secret check, roots, the temp file and a bundle the reader
accepts.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from whygraph.core.config import Config
from whygraph.core.context import ProjectContext, use_project
from whygraph.core.safe_paths import UnsafePathError
from whygraph.db import ensure_initialized, get_session
from whygraph.db.models import (
    ChatMessage,
    ChatSession,
    Commit,
    CommitFileChange,
    ImportedActor,
    ImportedChat,
    ProjectImport,
    ProjectNote,
    ProjectPurposeRun,
    RationaleCache,
)
from whygraph.portal import transfer_bundle as tb
from whygraph.portal import transfer_export as te
from whygraph.portal.config_layers import ConfigPolicyError
from whygraph.portal.models import Project
from whygraph.serve.chat import native_origin_key
from whygraph.serve.errors import ServeError

T0 = "2026-09-01T10:00:00+00:00"
T1 = "2026-09-02T10:00:00+00:00"
ROOT_OLD = "1" * 40
ROOT_NEW = "2" * 40
SHA = "3" * 40
STUB = "4" * 40
FP = "f" * 64
ME = "u-me"
OTHER = "u-other"
IMPORTED_ME = "imported:u-gone"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A bound project DB, a project row and stubbed portal reads."""
    root = tmp_path / "repo"
    root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    ctx = ProjectContext(
        slug="demo", root=root, config=Config(whygraph_db=tmp_path / "whygraph.db")
    )
    layer: dict[str, Any] = {}

    @contextmanager
    def fake_portal() -> Iterator[None]:
        yield None

    monkeypatch.setattr(te, "portal_session", fake_portal)
    monkeypatch.setattr(te, "load_layer", lambda db, pid, org_id: dict(layer))
    monkeypatch.setattr(
        te,
        "_user_names",
        lambda uids: {u: f"Name of {u}" for u in uids if u != "u-ghost"},
    )
    project = Project(
        id=1,
        org_id=1,
        slug="demo",
        name="Demo",
        source="local",
        root=str(root),
        remote_url="https://tok@github.com/Acme/Demo.git",
        last_scanned_head=SHA,
        default_branch="main",
    )
    with use_project(ctx):
        ensure_initialized()
        yield SimpleNamespace(
            root=root, data=data, ctx=ctx, project=project, layer=layer
        )


def _commit(sha: str, parents: str, at: str, **kw: Any) -> Commit:
    return Commit(
        sha=sha,
        parent_shas=parents,
        author_name="a",
        author_email="a@x",
        authored_at=at,
        committed_at=at,
        subject="s",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at=at,
        **kw,
    )


def _note(i: int, **kw: Any) -> ProjectNote:
    base: dict[str, Any] = dict(
        id=i,
        kind="decision",
        body=f"Note {i}.",
        status="confirmed",
        anchor_kind="project",
        source="human",
        proposed_by=ME,
        proposed_at=T0,
        confirmed_by=ME,
        confirmed_at=T1,
    )
    base.update(kw)
    return ProjectNote(**base)


def _seed(session: Any) -> None:
    session.add(
        ProjectImport(
            id=1,
            bundle_sha256="0" * 64,
            imported_by="u",
            imported_at=T0,
            source_mode="local",
            source_label="a/b",
            whygraph_version="x",
            counts="{}",
        )
    )
    session.commit()
    session.add(_commit(ROOT_NEW, "", T1))
    session.add(_commit(ROOT_OLD, "", T0))
    session.add(
        _commit(
            SHA,
            ROOT_OLD,
            T1,
            llm_description="Did a thing.",
            llm_description_model="anthropic:claude",
        )
    )
    session.add(
        _commit(STUB, SHA, T1, llm_description="stub", llm_description_model=None)
    )
    session.commit()
    session.add(
        CommitFileChange(
            commit_sha=SHA,
            path="a.py",
            change_type="M",
            llm_description="File text.",
            llm_description_model="anthropic:claude",
        )
    )
    session.add(
        CommitFileChange(
            commit_sha=SHA, path="b.py", change_type="M", llm_description="stub"
        )
    )
    session.add(
        RationaleCache(
            path="a.py",
            line_start=1,
            line_end=5,
            provider="anthropic",
            model="default",
            evidence_fingerprint=FP,
            cached_at=T1,
            purpose="p",
            why="w",
            constraints="[]",
            tradeoffs='["t"]',
            risks="[]",
            import_id=7,
            deferred_shas="abc1234 def5678",
        )
    )
    session.commit()


def _export(env: SimpleNamespace, **kw: Any) -> te.ExportFile:
    args: dict[str, Any] = dict(
        org_slug="local",
        uid=ME,
        include_chats=False,
        mode="local",
        data_dir=env.data,
    )
    args.update(kw)
    return te.build_export(env.project, **args)


def _read(file: te.ExportFile) -> tb.Bundle:
    return tb.read_bundle(file.path)


def recs(bundle: tb.Bundle, name: str) -> list[Any]:
    """The parsed records of one member."""
    return [record for _, record in bundle.iter_records(name)]


def test_reader_accepts_the_export(env: SimpleNamespace) -> None:
    with get_session() as s:
        _seed(s)
        s.add(_note(1))
        s.commit()
    env.layer.update({"llm": {"model": "m"}})
    file = _export(env)
    bundle = _read(file)
    assert bundle.manifest.repo.root_commits == [ROOT_OLD, ROOT_NEW]
    assert bundle.manifest.repo.origin == "github.com/acme/demo"
    assert bundle.manifest.repo.default_branch == "main"
    assert bundle.manifest.source.label == "local/demo"
    assert file.filename.startswith("whygraph-demo-")
    assert file.size == file.path.stat().st_size
    assert file.counts[tb.NOTES] == 1
    assert file.skipped == 0


def test_descriptions_skip_stubs_and_rationale_drops_import_id(
    env: SimpleNamespace,
) -> None:
    with get_session() as s:
        _seed(s)
    bundle = _read(_export(env))
    commits = recs(bundle, tb.COMMIT_DESCRIPTIONS)
    assert [c.sha for c in commits] == [SHA]
    files = recs(bundle, tb.FILE_DESCRIPTIONS)
    assert [(f.commit_sha, f.path) for f in files] == [(SHA, "a.py")]
    cards = recs(bundle, tb.RATIONALE)
    assert len(cards) == 1 and cards[0].model == "default"
    assert cards[0].deferred_shas == "abc1234 def5678"
    assert "import_id" not in tb.RationaleRecord.model_fields


def test_note_filter_and_edited_flag(env: SimpleNamespace) -> None:
    with get_session() as s:
        _seed(s)
        s.add(_note(1))
        s.add(_note(2, status="proposed", source="chat", confirmed_by=None))  # agent
        s.add(_note(3, status="proposed", source="mcp", confirmed_by=None))  # agent
        s.add(_note(4, status="proposed", source="human", confirmed_by=None))
        s.add(_note(5, status="proposed", source="analysis", confirmed_by=None))
        s.add(
            _note(
                6,
                status="retired",
                retire_reason="rejected",
                retired_by=ME,
                retired_at=T1,
            )
        )
        s.add(
            _note(
                7,
                status="retired",
                retire_reason="outdated",
                retired_by=ME,
                retired_at=T1,
            )
        )
        s.add(
            _note(
                8,
                status="retired",
                source="chat",
                retire_reason="wrong",
                retired_by=ME,
                retired_at=T1,
            )
        )
        s.add(_note(9, proposed_body="the original proposal text"))
        s.add(_note(10, supersedes_id=6, status="proposed", confirmed_by=None))
        s.commit()
    bundle = _read(_export(env))
    notes = {n.id: n for n in recs(bundle, tb.NOTES)}
    assert set(notes) == {1, 4, 5, 7, 8, 9, 10}
    assert notes[9].edited_on_confirm is True
    assert notes[1].edited_on_confirm is False
    assert "original proposal" not in json.dumps(
        [n.model_dump() for n in notes.values()]
    )
    # a supersede link to a left-out note is cut
    assert notes[10].supersedes_id is None


def test_a_constraint_note_exports_its_check(env: SimpleNamespace) -> None:
    check = {"check_kind": "present", "check_text": "with lock:", "check_glob": "**"}
    with get_session() as s:
        _seed(s)
        s.add(_note(1, kind="constraint", **check))
        s.add(_note(2))
        s.commit()
    bundle = _read(_export(env))
    notes = {n.id: n for n in recs(bundle, tb.NOTES)}
    assert {k: getattr(notes[1], k) for k in check} == check
    assert (notes[2].check_kind, notes[2].check_text, notes[2].check_glob) == (
        None,
        None,
        None,
    )


def test_uids_are_unprefixed_and_named_by_the_right_table(
    env: SimpleNamespace,
) -> None:
    with get_session() as s:
        _seed(s)
        s.add(ImportedActor(uid=IMPORTED_ME, display_name="Gone Person", import_id=1))
        # u-me also exists as a *plain* uid; an imported uid never borrows its name
        s.add(_note(1, proposed_by=IMPORTED_ME, confirmed_by=IMPORTED_ME))
        s.add(_note(2, proposed_by="u-ghost", confirmed_by=OTHER))
        s.add(_note(3, proposed_by="", confirmed_by=None, status="proposed"))
        s.commit()
    bundle = _read(_export(env))
    notes = {n.id: n for n in recs(bundle, tb.NOTES)}
    assert notes[1].proposed_by == "u-gone" and notes[1].confirmed_by == "u-gone"
    assert notes[3].proposed_by == ""
    actors = {a.uid: a.display_name for a in recs(bundle, tb.ACTORS)}
    assert actors == {
        "u-gone": "Gone Person",
        "u-ghost": te.FORMER_MEMBER,
        OTHER: f"Name of {OTHER}",
        ME: f"Name of {ME}",
    }


def test_prefixed_uid_without_actor_row_is_former_member(
    env: SimpleNamespace,
) -> None:
    """The portal users table is never consulted for an ``imported:`` uid."""
    with get_session() as s:
        _seed(s)
        s.add(_note(1, proposed_by="imported:u-x", confirmed_by="imported:u-x"))
        s.commit()
    bundle = _read(_export(env, uid=None))
    actors = {a.uid: a.display_name for a in recs(bundle, tb.ACTORS)}
    assert actors == {"u-x": te.FORMER_MEMBER}


def test_purpose_runs_need_an_exported_note(env: SimpleNamespace) -> None:
    def run(note_id: int | None, status: str = "ok") -> ProjectPurposeRun:
        return ProjectPurposeRun(
            requested_by="imported:u-r",
            started_at=T0,
            finished_at=T1,
            status=status,
            note_id=note_id,
        )

    with get_session() as s:
        _seed(s)
        s.add(_note(1, kind="purpose", source="analysis"))
        s.add(run(1))
        s.add(run(1, "failed"))
        s.add(run(99))
        s.add(run(None))
        s.commit()
    bundle = _read(_export(env))
    runs = recs(bundle, tb.PURPOSE_RUNS)
    assert len(runs) == 1 and runs[0].note_id == 1 and runs[0].requested_by == "u-r"


def _chat(s: Any, owner: str | None, title: str) -> ChatSession:
    row = ChatSession(
        title=title,
        provider="openai",
        model="m",
        created_at=T0,
        updated_at=T1,
        owner_uid=owner,
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    s.add(
        ChatMessage(
            session_id=row.id,
            role="user",
            content="hi",
            created_at=T0,
            rating=1,
            rating_comment="nice",
        )
    )
    s.add(
        ChatMessage(
            session_id=row.id,
            role="assistant",
            content="hello",
            created_at=T1,
            parent_id=None,
            tool_calls=json.dumps([{"id": "c1", "name": "t", "arguments": {"a": 1}}]),
            replay=json.dumps({"reasoning_content": "r", "other": "dropped"}),
        )
    )
    s.commit()
    return row


def test_chats_are_opt_in_and_own_only(env: SimpleNamespace) -> None:
    with get_session() as s:
        _seed(s)
        mine = _chat(s, ME, "mine")
        _chat(s, OTHER, "theirs")
        _chat(s, None, "ownerless")
        mine_key = native_origin_key(mine)
    assert tb.CHAT_SESSIONS not in _read(_export(env)).manifest.members

    bundle = _read(_export(env, include_chats=True, mode="production"))
    sessions = recs(bundle, tb.CHAT_SESSIONS)
    assert [(s.title, s.origin_key) for s in sessions] == [("mine", mine_key)]
    messages = recs(bundle, tb.CHAT_MESSAGES)
    assert len(messages) == 2
    assert messages[1].replay is not None
    assert messages[1].replay.reasoning_content == "r"
    assert messages[1].tool_calls[0].arguments == {"a": 1}
    assert "rating" not in tb.ChatMessageRecord.model_fields

    local = _read(_export(env, include_chats=True, mode="local"))
    assert [s.title for s in recs(local, tb.CHAT_SESSIONS)] == ["mine", "ownerless"]


def test_imported_chat_keeps_its_origin_key(env: SimpleNamespace) -> None:
    with get_session() as s:
        _seed(s)
        row = _chat(s, ME, "came from elsewhere")
        s.add(ImportedChat(origin_key="e" * 64, session_id=row.id, import_id=1))
        s.commit()
    bundle = _read(_export(env, include_chats=True))
    assert [s.origin_key for s in recs(bundle, tb.CHAT_SESSIONS)] == ["e" * 64]


def test_a_session_with_an_unwritable_message_stays_home(
    env: SimpleNamespace,
) -> None:
    with get_session() as s:
        _seed(s)
        bad = _chat(s, ME, "bad")
        _chat(s, ME, "good")
        s.add(
            ChatMessage(
                session_id=bad.id,
                role="assistant",
                content="x",
                created_at=T0,
                tool_calls=json.dumps([{"id": "c", "name": "t", "arguments": [1]}]),
            )
        )
        s.commit()
    file = _export(env, include_chats=True)
    bundle = _read(file)
    assert [s.title for s in recs(bundle, tb.CHAT_SESSIONS)] == ["good"]
    assert file.skipped == 1


def test_config_is_filtered_to_d15(env: SimpleNamespace) -> None:
    with get_session() as s:
        _seed(s)
    env.layer.update(
        {
            "llm": {"model": "m", "anthropic": {"model": "x", "api_key": "sk-secret"}},
            "scan": {"hooks": True, "token": "ghp_abcdefghijkl"},
            "rationale": {"max_evidence": 5},
            "areas": {"all": ["*"]},
        }
    )
    file = _export(env)
    config = _read(file).config
    assert "scan" not in config
    assert config["llm"]["model"] == "m"
    assert "api_key" not in json.dumps(config)
    assert "ghp_" not in json.dumps(config)
    assert "areas" in config


def test_secret_check_raises() -> None:
    with pytest.raises(ConfigPolicyError):
        te._secret_free({"llm": {"openai": {"api_key": "x"}}})
    with pytest.raises(ConfigPolicyError):
        te._secret_free({"areas": {"a": ["ghp_abcdefghijklmnop"]}})
    with pytest.raises(ConfigPolicyError):
        te._secret_free({"llm": {"model": "sk-ant-abcdefghijklmnopqrstuvwxyz"}})
    te._secret_free({"llm": {"model": "claude"}, "areas": {"a": ["src/*"]}})


def test_a_secret_in_the_layer_aborts_with_no_file(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_session() as s:
        _seed(s)
    monkeypatch.setattr(
        te, "_project_layer", lambda p, m: {"llm": {"openai": {"api_key": "k"}}}
    )
    with pytest.raises(ConfigPolicyError):
        _export(env)
    assert not (env.data / "tmp").exists() or not list((env.data / "tmp").iterdir())


def test_roots_are_oldest_first_and_capped(env: SimpleNamespace) -> None:
    with get_session() as s:
        for i in range(70):
            s.add(
                _commit(f"{i:040x}", "", f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00")
            )
        s.add(_commit(SHA, f"{0:040x}", T1))
        s.commit()
    roots = _read(_export(env)).manifest.repo.root_commits
    assert len(roots) == tb.MAX_ROOT_COMMITS
    dates = []
    with get_session() as s:
        for r in roots:
            dates.append(s.get(Commit, r).committed_at)
    assert dates == sorted(dates)


def test_needs_a_scan(env: SimpleNamespace) -> None:
    with pytest.raises(ServeError) as exc:
        _export(env)  # no commit rows
    assert exc.value.code == "transfer_needs_scan"
    env.project.last_scanned_head = None
    with pytest.raises(ServeError) as exc:
        _export(env)
    assert exc.value.code == "transfer_needs_scan"


def test_origin_of_falls_back_to_none(env: SimpleNamespace) -> None:
    env.project.remote_url = None
    env.project.source = "github"
    assert te.origin_of(env.project) is None
    env.project.remote_url = "git@github.com:Acme/Demo.git"
    assert te.origin_of(env.project) == "github.com/acme/demo"


def test_names_and_advisory_fields_are_sanitised_to_the_wire_rules(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_session() as s:
        _seed(s)
        s.commit()
    env.project.name = "Pay\u202bments\u2028 \u0007" + "x" * 300
    env.project.default_branch = "ma\u202ein\u2066"
    monkeypatch.setattr(te, "origin_of", lambda project: "h/o/" + "n" * 400)
    bundle = _read(_export(env))
    name = bundle.manifest.source.name
    assert name.startswith("Payments") and len(name) == 200
    assert all(tb._printable(c, 1) is not None for c in name)
    assert bundle.manifest.repo.default_branch == "main"
    assert bundle.manifest.repo.origin is None  # advisory: omitted, not cut

    env.project.default_branch = "\u202e"
    monkeypatch.setattr(te, "origin_of", lambda project: "h/o/n\u202e")
    bundle = _read(_export(env))
    assert bundle.manifest.repo.default_branch is None
    assert bundle.manifest.repo.origin == "h/o/n"


def test_a_superseded_note_that_fails_validation_does_not_break_the_import(
    env: SimpleNamespace,
) -> None:
    with get_session() as s:
        _seed(s)
        s.add(_note(1, refs=json.dumps([1])))  # not a list of strings
        s.add(_note(2, supersedes_id=1))
        s.add(_note(3))
        s.commit()
    file = _export(env)
    bundle = _read(file)
    notes = recs(bundle, tb.NOTES)
    assert [n.id for n in notes] == [2, 3]
    assert notes[0].supersedes_id is None
    ids = {n.id for n in notes}
    # The importer's bundle rule: a predecessor is an earlier note of the bundle.
    assert all(
        n.supersedes_id is None or (n.supersedes_id < n.id and n.supersedes_id in ids)
        for n in notes
    )
    assert file.skipped >= 1


def test_temp_file_is_private(env: SimpleNamespace) -> None:
    with get_session() as s:
        _seed(s)
    file = _export(env)
    assert file.path.parent == env.data / "tmp"
    assert stat.S_IMODE(file.path.stat().st_mode) == 0o600
    assert stat.S_IMODE((env.data / "tmp").stat().st_mode) == 0o700
    assert len(file.sha256) == 64


def test_symlinked_tmp_is_refused(env: SimpleNamespace, tmp_path: Path) -> None:
    with get_session() as s:
        _seed(s)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, env.data / "tmp")
    with pytest.raises(UnsafePathError):
        _export(env)
    assert list(elsewhere.iterdir()) == []


def test_over_the_file_cap_removes_the_file(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with get_session() as s:
        _seed(s)
    monkeypatch.setattr(tb, "MAX_BUNDLE_BYTES", 10)
    with pytest.raises(tb.BundleError) as exc:
        _export(env)
    assert exc.value.code == "bundle_too_large" and exc.value.status == 413
    assert list((env.data / "tmp").iterdir()) == []
