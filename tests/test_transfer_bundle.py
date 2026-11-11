"""The project-transfer bundle format (plan 42 sections 5.2, 7.1 and 10).

A round trip through :func:`write_bundle` / :func:`read_bundle`, then one
test per refusal of the reader: the end-of-central-directory pre-check
(before ``ZipFile`` is built), the name and entry rules, the verification
pass (sizes, hashes, rows, CRC, line and byte caps), the version check and
the record parse. Hostile archives are built by hand with :mod:`zipfile`
and patched with :mod:`struct`. The column inventory test pins every
exported table to its wire model (D7).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import zipfile
from pathlib import Path
from typing import Any

import pytest

from whygraph.db import models as db_models
from whygraph.db.base import metadata
from whygraph.portal import transfer_bundle as tb
from whygraph.portal.transfer_bundle import (
    ActorRecord,
    BundleError,
    ChatMessageRecord,
    ChatSessionRecord,
    CommitDescriptionRecord,
    FileDescriptionRecord,
    Manifest,
    NoteRecord,
    PurposeRunRecord,
    RationaleRecord,
    read_bundle,
    write_bundle,
)

ROOT = "a" * 40
SHA = "b" * 40
KEY = "c" * 64


def manifest() -> Manifest:
    """A manifest before the writer fills in members and includes."""
    return Manifest(
        format="whygraph-project-bundle",
        format_version=1,
        whygraph_version="2.2.1",
        schema_revision="71460539b46f",
        exported_at="2026-11-01T10:00:00Z",
        source={"mode": "local", "label": "local/demo", "name": "demo"},
        repo={"root_commits": [ROOT], "origin": "github.com/a/b"},
    )


def note(note_id: int = 1, **over: Any) -> NoteRecord:
    data: dict[str, Any] = {
        "id": note_id,
        "kind": "decision",
        "body": "Keep the cache key content-addressable.",
        "status": "confirmed",
        "anchor_kind": "path",
        "anchor_path": "src/a.py",
        "source": "human",
        "refs": [SHA, "#12"],
        "proposed_by": "u1",
        "proposed_at": "2026-10-01T10:00:00Z",
        "confirmed_by": "u2",
        "confirmed_at": "2026-10-02T10:00:00Z",
    }
    data.update(over)
    return NoteRecord(**data)


def members(*, chats: bool = False, config: dict | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        tb.ACTORS: [ActorRecord(uid="u1", display_name="Ann")],
        tb.NOTES: [note(1), note(2, supersedes_id=1, status="proposed")],
        tb.PURPOSE_RUNS: [
            PurposeRunRecord(
                requested_by="u1",
                started_at="2026-10-01T10:00:00Z",
                finished_at="2026-10-01T10:00:05Z",
                status="ok",
                sources=["README.md"],
                note_id=1,
            )
        ],
        tb.RATIONALE: [
            RationaleRecord(
                path="src/a.py",
                line_start=1,
                line_end=20,
                provider="anthropic",
                model="default",
                evidence_fingerprint=KEY,
                cached_at="2026-10-01T10:00:00Z",
                purpose="p",
                why="w",
                constraints=["c"],
                tradeoffs=[],
                risks=["r"],
            )
        ],
        tb.COMMIT_DESCRIPTIONS: [
            CommitDescriptionRecord(
                sha=SHA, llm_description="déjà", llm_description_model="a:m"
            )
        ],
        tb.FILE_DESCRIPTIONS: [
            FileDescriptionRecord(
                commit_sha=SHA,
                path="src/a.py",
                llm_description="d",
                llm_description_model="a:m",
            )
        ],
    }
    if chats:
        out[tb.CHAT_SESSIONS] = [
            ChatSessionRecord(
                id=7,
                title="t",
                provider="anthropic",
                model="m",
                created_at="2026-10-01T10:00:00Z",
                updated_at="2026-10-01T10:00:00Z",
                active_leaf_id=11,
                origin_key=KEY,
            )
        ]
        out[tb.CHAT_MESSAGES] = [
            ChatMessageRecord(
                id=10,
                session_id=7,
                role="user",
                content="hi",
                created_at="2026-10-01T10:00:00Z",
            ),
            ChatMessageRecord(
                id=11,
                session_id=7,
                parent_id=10,
                role="assistant",
                content="hello",
                tool_calls=[{"id": "c1", "name": "find", "arguments": {"q": [1]}}],
                replay={"reasoning_content": "r"},
                created_at="2026-10-01T10:00:01Z",
            ),
        ]
    if config is not None:
        out[tb.CONFIG] = config
    return out


def written(tmp_path: Path, **kw: Any) -> Path:
    path = tmp_path / "bundle.zip"
    write_bundle(path, manifest(), members(**kw))
    return path


# --- raw archives ----------------------------------------------------------


def jsonl(*records: dict) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


def raw_members(**over: bytes) -> dict[str, bytes]:
    """Every core member, empty, plus ``over`` (``notes`` -> ``notes.jsonl``)."""
    data = {name: b"" for name in tb.CORE_MEMBERS}
    for key, value in over.items():
        data[f"{key}.jsonl" if f"{key}.jsonl" in tb.MEMBERS else key] = value
    return data


def info_for(data: dict[str, bytes], name: str) -> dict[str, Any]:
    body = data[name]
    rows = 1 if name == tb.CONFIG else body.count(b"\n")
    return {"sha256": hashlib.sha256(body).hexdigest(), "rows": rows}


def raw_manifest(data: dict[str, bytes], **over: Any) -> dict[str, Any]:
    out = json.loads(manifest().model_dump_json())
    out["members"] = {name: info_for(data, name) for name in data}
    out["includes"] = {
        "chats": tb.CHAT_SESSIONS in data,
        "config": tb.CONFIG in data,
    }
    out.update(over)
    return out


def build(
    path: Path,
    data: dict[str, bytes],
    *,
    manifest_obj: dict | bytes | None = None,
    compression: int = zipfile.ZIP_STORED,
    extra_entries: list[tuple[str, bytes]] = (),
    tweak: Any = None,
) -> Path:
    """Write a zip by hand; ``tweak(info)`` may edit each entry before writing."""
    if manifest_obj is None:
        manifest_obj = raw_manifest(data)
    mbytes = (
        manifest_obj
        if isinstance(manifest_obj, bytes)
        else json.dumps(manifest_obj).encode()
    )
    entries = [("manifest.json", mbytes), *data.items(), *extra_entries]
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = compression
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            if tweak is not None:
                tweak(info)
            zf.writestr(info, body)
    return path


def cd_entry(raw: bytes, name: str) -> int:
    """The offset of ``name``'s central-directory entry."""
    pos = raw.find(b"PK\x01\x02")
    while pos >= 0:
        nlen = struct.unpack_from("<H", raw, pos + 28)[0]
        if raw[pos + 46 : pos + 46 + nlen] == name.encode():
            return pos
        pos = raw.find(b"PK\x01\x02", pos + 4)
    raise AssertionError(name)


def eocd(raw: bytes) -> int:
    return raw.rfind(b"PK\x05\x06")


def refused(path: Path, code: str = "bundle_invalid") -> BundleError:
    with pytest.raises(BundleError) as caught:
        read_bundle(path)
    assert caught.value.code == code, str(caught.value)
    assert caught.value.status == (413 if code == "bundle_too_large" else 400)
    return caught.value


@pytest.fixture
def no_zipfile(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``refused`` with ``ZipFile`` patched out: the pre-check must refuse first."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("ZipFile() built before the pre-check refused")

    def check(path: Path) -> BundleError:
        with monkeypatch.context() as patch:
            patch.setattr(tb.zipfile, "ZipFile", boom)
            return refused(path)

    return check


# --- round trip --------------------------------------------------------------


def test_round_trip_with_chats_and_config(tmp_path: Path) -> None:
    config = {"llm": {"model": "m"}, "areas": {"core": {"paths": ["src/"]}}}
    path = tmp_path / "b.zip"
    final = write_bundle(path, manifest(), members(chats=True, config=config))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert final.includes.chats and final.includes.config
    source = members(chats=True, config=config)
    with read_bundle(path) as bundle:
        assert bundle.manifest == final
        assert bundle.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert bundle.config == config
        for name, model in tb.MEMBER_MODELS.items():
            got = list(bundle.iter_records(name))
            assert [line for line, _ in got] == list(range(1, len(got) + 1))
            assert [r for _, r in got] == source[name]
            assert all(isinstance(r, model) for _, r in got)
            assert bundle.rows(name) == len(got)


def test_round_trip_without_optional_members(tmp_path: Path) -> None:
    with read_bundle(written(tmp_path)) as bundle:
        assert not bundle.manifest.includes.chats
        assert not bundle.manifest.includes.config
        assert bundle.config is None
        assert list(bundle.iter_records(tb.CHAT_SESSIONS)) == []
        assert set(bundle.manifest.members) == set(tb.CORE_MEMBERS)


def test_reader_never_extracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("extract called")

    monkeypatch.setattr(zipfile.ZipFile, "extract", boom)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", boom)
    with read_bundle(written(tmp_path, chats=True, config={})) as bundle:
        for name in tb.MEMBER_MODELS:
            list(bundle.iter_records(name))


def test_extra_fields_are_ignored(tmp_path: Path) -> None:
    record = json.loads(note().model_dump_json())
    record["added_in_a_later_version"] = {"x": 1}
    data = raw_members(notes=jsonl(record))
    m = raw_manifest(data, some_new_key=True)
    m["source"]["also_new"] = 1
    with read_bundle(build(tmp_path / "b.zip", data, manifest_obj=m)) as bundle:
        [(_, got)] = list(bundle.iter_records(tb.NOTES))
        assert got == note()


def test_duplicate_json_keys_last_one_wins(tmp_path: Path) -> None:
    line = note().model_dump_json()[:-1].encode() + b', "kind": "gotcha"}\n'
    data = raw_members(notes=line)
    with read_bundle(build(tmp_path / "b.zip", data)) as bundle:
        [(_, got)] = list(bundle.iter_records(tb.NOTES))
        assert got.kind == "gotcha"


# --- writer ----------------------------------------------------------------------


def test_writer_refuses_an_existing_path(tmp_path: Path) -> None:
    path = tmp_path / "b.zip"
    path.write_bytes(b"x")
    with pytest.raises(FileExistsError):
        write_bundle(path, manifest(), members())
    assert path.read_bytes() == b"x"


def test_writer_refuses_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").write_bytes(b"")
    link = tmp_path / "b.zip"
    link.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        write_bundle(link, manifest(), members())
    assert (tmp_path / "elsewhere").read_bytes() == b""


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda m: m.pop(tb.NOTES), "bundle_invalid"),
        (lambda m: m.pop(tb.CHAT_MESSAGES), "bundle_invalid"),
        (lambda m: m.__setitem__("other.jsonl", []), "bundle_invalid"),
        (
            lambda m: m.__setitem__(tb.NOTES, [ActorRecord(uid="u", display_name="n")]),
            "bundle_invalid",
        ),
        (lambda m: m.__setitem__(tb.CONFIG, {"x": float("nan")}), "bundle_invalid"),
    ],
)
def test_writer_refusals_leave_no_file(tmp_path: Path, change: Any, code: str) -> None:
    data = members(chats=True)
    change(data)
    path = tmp_path / "b.zip"
    with pytest.raises(BundleError) as caught:
        write_bundle(path, manifest(), data)
    assert caught.value.code == code
    assert not path.exists()


def test_writer_enforces_the_row_and_line_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(tb.ROW_CAPS, tb.NOTES, 1)
    with pytest.raises(BundleError) as caught:
        write_bundle(tmp_path / "a.zip", manifest(), members())
    assert caught.value.code == "bundle_too_large"
    monkeypatch.setitem(tb.ROW_CAPS, tb.NOTES, 5_000)
    monkeypatch.setattr(tb, "MAX_LINE_BYTES", 100)
    with pytest.raises(BundleError) as caught:
        write_bundle(tmp_path / "b.zip", manifest(), members())
    assert caught.value.code == "bundle_too_large"
    assert not (tmp_path / "a.zip").exists() and not (tmp_path / "b.zip").exists()


# --- step 0: the end-of-central-directory pre-check --------------------------


def test_not_a_zip(tmp_path: Path, no_zipfile: Any) -> None:
    path = tmp_path / "b.zip"
    path.write_bytes(os.urandom(4096).replace(b"PK", b"pk"))
    no_zipfile(path)
    (tmp_path / "tiny.zip").write_bytes(b"PK")
    no_zipfile(tmp_path / "tiny.zip")


def test_more_entries_than_members(tmp_path: Path, no_zipfile: Any) -> None:
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(len(tb.MEMBERS) + 1):
            zf.writestr(f"e{i}", b"")
    assert "more than" in str(no_zipfile(path))


def test_a_hundred_thousand_entries(tmp_path: Path, no_zipfile: Any) -> None:
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(100_000):
            zf.writestr(str(i), b"")
    no_zipfile(path)


def test_zip64_locator(tmp_path: Path, no_zipfile: Any) -> None:
    raw = (build(tmp_path / "a.zip", raw_members())).read_bytes()
    at = eocd(raw)
    locator = b"PK\x06\x07" + b"\0" * 16
    path = tmp_path / "b.zip"
    path.write_bytes(raw[:at] + locator + raw[at:])
    assert "ZIP64" in str(no_zipfile(path))


def test_zip64_sentinel_counts(tmp_path: Path, no_zipfile: Any) -> None:
    raw = bytearray(build(tmp_path / "a.zip", raw_members()).read_bytes())
    struct.pack_into("<HH", raw, eocd(raw) + 8, 0xFFFF, 0xFFFF)
    path = tmp_path / "b.zip"
    path.write_bytes(bytes(raw))
    assert "ZIP64" in str(no_zipfile(path))


def test_archive_comment(tmp_path: Path, no_zipfile: Any) -> None:
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", b"{}")
        zf.comment = b"hello"
    assert "comment" in str(no_zipfile(path))


def test_trailing_data(tmp_path: Path, no_zipfile: Any) -> None:
    path = written(tmp_path)
    path.write_bytes(path.read_bytes() + b"\0" * 10)
    no_zipfile(path)


def test_central_directory_over_64_kib(tmp_path: Path, no_zipfile: Any) -> None:
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name in (tb.MANIFEST, tb.NOTES):
            info = zipfile.ZipInfo(name)
            info.comment = b"x" * 40_000
            zf.writestr(info, b"")
    assert "central directory" in str(no_zipfile(path))


def test_junk_prefix(tmp_path: Path, no_zipfile: Any) -> None:
    raw = written(tmp_path).read_bytes()
    path = tmp_path / "b.zip"
    path.write_bytes(b"junk" + raw)
    no_zipfile(path)


def test_junk_prefix_with_absolute_offsets(tmp_path: Path, no_zipfile: Any) -> None:
    path = tmp_path / "b.zip"
    with path.open("wb") as fh:
        fh.write(b"#!/bin/sh\nexit 0\n")
        with zipfile.ZipFile(fh, "w") as zf:
            zf.writestr("manifest.json", b"{}")
    assert "start" in str(no_zipfile(path))


def test_multi_disk(tmp_path: Path, no_zipfile: Any) -> None:
    raw = bytearray(written(tmp_path).read_bytes())
    struct.pack_into("<H", raw, eocd(raw) + 4, 1)
    path = tmp_path / "b.zip"
    path.write_bytes(bytes(raw))
    no_zipfile(path)


def test_eocd_entry_count_lies(tmp_path: Path) -> None:
    raw = bytearray(written(tmp_path).read_bytes())
    at = eocd(raw)
    count = struct.unpack_from("<H", raw, at + 10)[0]
    struct.pack_into("<HH", raw, at + 8, count - 1, count - 1)
    path = tmp_path / "b.zip"
    path.write_bytes(bytes(raw))
    assert "entry count" in str(refused(path))


def test_file_over_the_upload_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = written(tmp_path)
    monkeypatch.setattr(tb, "MAX_BUNDLE_BYTES", path.stat().st_size - 1)
    refused(path, "bundle_too_large")


def test_symlink_and_non_regular_paths(tmp_path: Path) -> None:
    target = written(tmp_path)
    link = tmp_path / "link.zip"
    link.symlink_to(target)
    refused(link)
    refused(tmp_path)
    refused(tmp_path / "missing.zip")


# --- step 1: names ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "other.jsonl",
        "../notes.jsonl",
        "a/notes.jsonl",
        "notes/",
        ".notes.jsonl",
        "notes.jsonl\\x",
    ],
)
def test_bad_member_names(tmp_path: Path, name: str) -> None:
    path = build(tmp_path / "b.zip", raw_members(), extra_entries=[(name, b"")])
    assert "member name" in str(refused(path))


def test_duplicate_member_name(tmp_path: Path) -> None:
    data = raw_members()
    with pytest.warns(UserWarning, match="Duplicate name"):
        path = build(tmp_path / "b.zip", data, extra_entries=[(tb.NOTES, b"")])
    assert "repeats" in str(refused(path))


def test_no_manifest(tmp_path: Path) -> None:
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(tb.NOTES, b"")
    refused(path)


def test_empty_archive(tmp_path: Path) -> None:
    path = tmp_path / "b.zip"
    with zipfile.ZipFile(path, "w"):
        pass
    refused(path)


def test_member_set_mismatch(tmp_path: Path) -> None:
    data = raw_members()
    m = raw_manifest(data)
    m["members"][tb.CONFIG] = {"sha256": KEY, "rows": 1}
    m["includes"]["config"] = True
    refused(build(tmp_path / "a.zip", data, manifest_obj=m))
    m = raw_manifest(data)
    refused(
        build(
            tmp_path / "b.zip", data, manifest_obj=m, extra_entries=[(tb.CONFIG, b"{}")]
        )
    )


def test_manifest_names_an_unknown_member(tmp_path: Path) -> None:
    data = raw_members()
    m = raw_manifest(data)
    m["members"]["manifest.json"] = {"sha256": KEY, "rows": 0}
    refused(build(tmp_path / "b.zip", data, manifest_obj=m))


def test_missing_core_member(tmp_path: Path) -> None:
    data = raw_members()
    del data[tb.RATIONALE]
    assert "rationale" in str(refused(build(tmp_path / "b.zip", data)))


@pytest.mark.parametrize("member", [tb.CHAT_SESSIONS, tb.CONFIG])
def test_includes_must_match_the_members(tmp_path: Path, member: str) -> None:
    data = raw_members(**{member: b"{}" if member == tb.CONFIG else b""})
    if member == tb.CHAT_SESSIONS:
        data[tb.CHAT_MESSAGES] = b""
    m = raw_manifest(data)
    m["includes"] = {"chats": False, "config": False}
    refused(build(tmp_path / "a.zip", data, manifest_obj=m))


def test_one_chat_member_alone(tmp_path: Path) -> None:
    data = raw_members(**{tb.CHAT_SESSIONS: b""})
    refused(build(tmp_path / "b.zip", data))


# --- step 2: entries ---------------------------------------------------------------


def test_symlink_entry(tmp_path: Path) -> None:
    def link(info: zipfile.ZipInfo) -> None:
        if info.filename == tb.NOTES:
            info.external_attr = (stat.S_IFLNK | 0o777) << 16

    path = build(tmp_path / "b.zip", raw_members(), tweak=link)
    assert "symlink" in str(refused(path))


def test_bzip2_member(tmp_path: Path) -> None:
    path = build(tmp_path / "b.zip", raw_members(), compression=zipfile.ZIP_BZIP2)
    assert "compression" in str(refused(path))


def test_encrypted_member(tmp_path: Path) -> None:
    path = build(tmp_path / "b.zip", raw_members())
    raw = bytearray(path.read_bytes())
    at = cd_entry(raw, tb.NOTES)
    flags = struct.unpack_from("<H", raw, at + 8)[0]
    struct.pack_into("<H", raw, at + 8, flags | 0x1)
    path.write_bytes(bytes(raw))
    assert "encrypted" in str(refused(path))


def test_zip64_extra_on_an_entry(tmp_path: Path) -> None:
    def zip64(info: zipfile.ZipInfo) -> None:
        if info.filename == tb.NOTES:
            info.extra = struct.pack("<HHQQ", 1, 16, 0, 0)

    path = build(tmp_path / "b.zip", raw_members(), tweak=zip64)
    assert "ZIP64" in str(refused(path))


def test_overlapping_entries(tmp_path: Path) -> None:
    path = build(tmp_path / "b.zip", raw_members())
    raw = bytearray(path.read_bytes())
    target = struct.unpack_from("<L", raw, cd_entry(raw, tb.ACTORS) + 42)[0]
    struct.pack_into("<L", raw, cd_entry(raw, tb.NOTES) + 42, target)
    path.write_bytes(bytes(raw))
    assert "overlap" in str(refused(path))


# --- steps 3-4: manifest, caps and the verification pass --------------------------


def test_row_cap_checked_before_the_member_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = raw_members()
    m = raw_manifest(data)
    m["members"][tb.NOTES]["rows"] = tb.ROW_CAPS[tb.NOTES] + 1
    path = build(tmp_path / "b.zip", data, manifest_obj=m)
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self: zipfile.ZipFile, name: Any, *a: Any, **k: Any) -> Any:
        opened.append(getattr(name, "filename", name))
        return real_open(self, name, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    refused(path, "bundle_too_large")
    assert opened == [tb.MANIFEST]


def test_declared_size_over_the_uncompressed_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = written(tmp_path)
    monkeypatch.setattr(tb, "MAX_UNCOMPRESSED_BYTES", 200)
    refused(path, "bundle_too_large")


def test_bytes_actually_read_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr(tb, "MAX_UNCOMPRESSED_BYTES", 100)
    total = [0]
    with pytest.raises(BundleError) as caught:
        list(tb._chunks(io.BytesIO(b"x" * 101), total))
    assert caught.value.code == "bundle_too_large"


def _transaction_bytes(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        return sum(
            info.file_size
            for info in zf.infolist()
            if info.filename in tb.TRANSACTION_MEMBERS
        )


def test_the_transaction_members_and_their_budget() -> None:
    assert set(tb.TRANSACTION_MEMBERS) == {
        tb.ACTORS,
        tb.NOTES,
        tb.PURPOSE_RUNS,
        tb.CHAT_SESSIONS,
        tb.CHAT_MESSAGES,
    }
    assert tb.MAX_TRANSACTION_BYTES < tb.MAX_UNCOMPRESSED_BYTES


def test_the_writer_enforces_the_transaction_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exact = _transaction_bytes(written(tmp_path, chats=True))
    monkeypatch.setattr(tb, "MAX_TRANSACTION_BYTES", exact)
    write_bundle(tmp_path / "fits.zip", manifest(), members(chats=True))
    monkeypatch.setattr(tb, "MAX_TRANSACTION_BYTES", exact - 1)
    path = tmp_path / "over.zip"
    with pytest.raises(BundleError) as caught:
        write_bundle(path, manifest(), members(chats=True))
    assert caught.value.code == "bundle_too_large"
    assert not path.exists()


def test_cache_members_do_not_count_toward_the_transaction_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = members()
    data[tb.COMMIT_DESCRIPTIONS] = [
        CommitDescriptionRecord(
            sha=f"{i:040x}", llm_description="d" * 1000, llm_description_model="a:m"
        )
        for i in range(50)
    ]
    path = tmp_path / "b.zip"
    write_bundle(path, manifest(), data)
    monkeypatch.setattr(tb, "MAX_TRANSACTION_BYTES", _transaction_bytes(path))
    read_bundle(path).close()


def test_declared_transaction_bytes_over_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = written(tmp_path, chats=True)
    monkeypatch.setattr(tb, "MAX_TRANSACTION_BYTES", _transaction_bytes(path) - 1)
    opened: list[str] = []
    real_open = zipfile.ZipFile.open

    def spy(self: zipfile.ZipFile, name: Any, *a: Any, **k: Any) -> Any:
        opened.append(getattr(name, "filename", name))
        return real_open(self, name, *a, **k)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    refused(path, "bundle_too_large")
    assert opened == []


def test_transaction_bytes_actually_read_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    path = written(tmp_path, chats=True)
    sizes = _transaction_bytes(path)
    monkeypatch.setattr(tb, "MAX_TRANSACTION_BYTES", 100)
    core = [60]
    with pytest.raises(BundleError) as caught:
        list(tb._chunks(io.BytesIO(b"x" * 41), [0], core))
    assert caught.value.code == "bundle_too_large"
    # The verification pass counts every transaction member into one budget.
    real_check = tb._check_entries

    def lenient(zf: zipfile.ZipFile, total: int) -> Any:
        with monkeypatch.context() as patch:
            patch.setattr(tb, "MAX_TRANSACTION_BYTES", 10 * sizes)
            return real_check(zf, total)

    monkeypatch.setattr(tb, "_check_entries", lenient)
    monkeypatch.setattr(tb, "MAX_TRANSACTION_BYTES", sizes - 1)
    refused(path, "bundle_too_large")
    monkeypatch.setattr(tb, "MAX_TRANSACTION_BYTES", sizes)
    read_bundle(path).close()


def test_a_zip_bomb_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 4 MiB of one byte deflates to a few KiB; the line cap stops it.
    data = raw_members(notes=b"1" * (4 * tb.MIB) + b"\n")
    path = build(tmp_path / "b.zip", data, compression=zipfile.ZIP_DEFLATED)
    assert path.stat().st_size < 64 * 1024
    monkeypatch.setattr(tb, "MAX_LINE_BYTES", 1 * tb.MIB)
    refused(path, "bundle_too_large")


def test_a_line_over_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = written(tmp_path)
    monkeypatch.setattr(tb, "MAX_LINE_BYTES", 100)
    refused(path, "bundle_too_large")


def test_a_line_over_the_real_cap(tmp_path: Path) -> None:
    data = raw_members(notes=b"x" * tb.MAX_LINE_BYTES + b"\n")
    path = build(tmp_path / "b.zip", data, compression=zipfile.ZIP_DEFLATED)
    refused(path, "bundle_too_large")


def test_file_size_lies_small(tmp_path: Path) -> None:
    data = raw_members(notes=jsonl(json.loads(note().model_dump_json())))
    path = build(tmp_path / "b.zip", data, compression=zipfile.ZIP_DEFLATED)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<L", raw, cd_entry(raw, tb.NOTES) + 24, 10)
    path.write_bytes(bytes(raw))
    assert "notes.jsonl cannot be read" in str(refused(path))


def test_file_size_lies_large(tmp_path: Path) -> None:
    data = raw_members(notes=jsonl(json.loads(note().model_dump_json())))
    path = build(tmp_path / "b.zip", data, compression=zipfile.ZIP_DEFLATED)
    raw = bytearray(path.read_bytes())
    at = cd_entry(raw, tb.NOTES)
    size = struct.unpack_from("<L", raw, at + 24)[0]
    struct.pack_into("<L", raw, at + 24, size + 1000)
    path.write_bytes(bytes(raw))
    assert "size" in str(refused(path))


def test_file_size_lies_huge(tmp_path: Path) -> None:
    path = build(tmp_path / "b.zip", raw_members(notes=b"\n"))
    raw = bytearray(path.read_bytes())
    struct.pack_into("<L", raw, cd_entry(raw, tb.NOTES) + 24, 0xFFFF_FFF0)
    path.write_bytes(bytes(raw))
    refused(path, "bundle_too_large")


def test_crc_error_surfaces_before_any_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = jsonl(json.loads(note().model_dump_json()))
    data = raw_members(notes=body)
    path = build(tmp_path / "b.zip", data)  # stored: a flipped byte stays a byte
    raw = path.read_bytes()
    at = raw.index(b'"Keep the cache')
    path.write_bytes(raw[:at] + b'"Kepp' + raw[at + 5 :])
    parsed: list[bytes] = []
    real = NoteRecord.model_validate_json

    def spy(data: Any, *a: Any, **k: Any) -> Any:
        parsed.append(data)
        return real(data, *a, **k)

    monkeypatch.setattr(NoteRecord, "model_validate_json", spy)
    assert "notes.jsonl cannot be read" in str(refused(path))
    assert parsed == []


def test_hash_mismatch(tmp_path: Path) -> None:
    data = raw_members(notes=b"{}\n")
    m = raw_manifest(data)
    m["members"][tb.NOTES]["sha256"] = KEY
    assert "sha256" in str(refused(build(tmp_path / "b.zip", data, manifest_obj=m)))


@pytest.mark.parametrize("rows", [0, 2])
def test_row_count_mismatch(tmp_path: Path, rows: int) -> None:
    data = raw_members(notes=b"{}\n")
    m = raw_manifest(data)
    m["members"][tb.NOTES]["rows"] = rows
    refused(build(tmp_path / "b.zip", data, manifest_obj=m))


@pytest.mark.parametrize("body", [b"{}\n\n{}\n", b"{}\n{}", b"  \n"])
def test_blank_or_unterminated_lines(tmp_path: Path, body: bytes) -> None:
    data = raw_members(notes=body)
    m = raw_manifest(data)
    m["members"][tb.NOTES]["rows"] = 2
    refused(build(tmp_path / "b.zip", data, manifest_obj=m))


def test_config_rows_must_be_one(tmp_path: Path) -> None:
    data = raw_members(**{tb.CONFIG: b"{}"})
    m = raw_manifest(data)
    m["members"][tb.CONFIG]["rows"] = 0
    refused(build(tmp_path / "b.zip", data, manifest_obj=m))


def test_manifest_over_64_kib(tmp_path: Path) -> None:
    data = raw_members()
    m = raw_manifest(data, padding="x" * (64 * 1024))
    assert "manifest.json is over" in str(
        refused(build(tmp_path / "b.zip", data, manifest_obj=m))
    )


def test_config_over_64_kib(tmp_path: Path) -> None:
    data = raw_members(**{tb.CONFIG: json.dumps({"a": "x" * 70_000}).encode()})
    refused(build(tmp_path / "b.zip", data))


# --- versions and the manifest ---------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"format_version": 2},
        {"format_version": "1"},
        {"format_version": True},
        {"format": "something-else"},
    ],
)
def test_bundle_version(tmp_path: Path, over: dict) -> None:
    data = raw_members()
    m = raw_manifest(data, whygraph_version="9.9.9", **over)
    err = refused(build(tmp_path / "b.zip", data, manifest_obj=m), "bundle_version")
    assert "9.9.9" in str(err)
    assert tb._whygraph_version() in str(err)


def test_bundle_version_never_echoes_a_hostile_version(tmp_path: Path) -> None:
    data = raw_members()
    m = raw_manifest(data, format_version=2, whygraph_version="‮evil")
    err = refused(build(tmp_path / "b.zip", data, manifest_obj=m), "bundle_version")
    assert "evil" not in str(err)


@pytest.mark.parametrize(
    "path_value",
    [
        ("repo", "root_commits", []),
        ("repo", "root_commits", ["A" * 40]),
        ("repo", "root_commits", [ROOT, ROOT]),
        ("repo", "root_commits", [f"{i:040x}" for i in range(65)]),
        ("source", "mode", "cloud"),
        ("source", "label", "bad\nlabel"),
        ("source", "name", "x⁦y"),
        ("source", "name", "x\u200fy"),
        ("source", "name", "x\u200ey"),
        ("source", "name", "x\u061cy"),
        ("source", "label", "x\u2028y"),
        ("source", "label", "x\u2029y"),
        ("source", "name", "x" * 201),
        (None, "exported_at", "yesterday"),
        (None, "whygraph_version", 2),
    ],
)
def test_manifest_field_rules(tmp_path: Path, path_value: tuple) -> None:
    section, key, value = path_value
    data = raw_members()
    m = raw_manifest(data)
    (m[section] if section else m)[key] = value
    refused(build(tmp_path / "b.zip", data, manifest_obj=m))


@pytest.mark.parametrize(
    "body",
    [b"[]", b"not json", b"\xff\xfe{}", b"[" * 5000 + b"]" * 5000],
)
def test_manifest_not_an_object(tmp_path: Path, body: bytes) -> None:
    refused(build(tmp_path / "b.zip", raw_members(), manifest_obj=body))


# --- step 5: parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        b'{"a": ' + b"[" * 5000 + b"]" * 5000 + b"}",
        b'{"a": ' + b"[" * 9 + b"]" * 9 + b"}",
        b'{"a": ' + b"1" * 5000 + b"}",
        b'{"a": 4294967296}',
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": 1e999}',
        b"[1]",
        b'{"bad\\nkey": 1}',
        b'{"a": "\xff"}',
    ],
)
def test_config_refusals(tmp_path: Path, config: bytes) -> None:
    data = raw_members(**{tb.CONFIG: config})
    assert "config.json" in str(refused(build(tmp_path / "b.zip", data)))


def iter_refused(path: Path, member: str) -> str:
    with read_bundle(path) as bundle, pytest.raises(BundleError) as caught:
        list(bundle.iter_records(member))
    assert caught.value.code == "bundle_invalid"
    assert caught.value.status == 400
    return str(caught.value)


SECRET = "sekrit-value-do-not-echo"


@pytest.mark.parametrize(
    "change",
    [
        {"id": 2**31 + 1},
        {"id": -1},
        {"id": "1"},
        {"id": 1.0},
        {"line_start": 2**63},
        {"kind": 5},
        {"edited_on_confirm": 1},
        {"refs": SECRET},
        {"body": None},
    ],
)
def test_record_refusals_name_member_and_line(tmp_path: Path, change: dict) -> None:
    good = json.loads(note().model_dump_json())
    bad = {**good, **change}
    data = raw_members(notes=jsonl(good, bad))
    message = iter_refused(build(tmp_path / "b.zip", data), tb.NOTES)
    assert "notes.jsonl line 2" in message
    assert SECRET not in message


def test_records_before_the_bad_line_are_yielded(tmp_path: Path) -> None:
    good = json.loads(note().model_dump_json())
    data = raw_members(notes=jsonl(good, {"id": "x"}))
    with read_bundle(build(tmp_path / "b.zip", data)) as bundle:
        seen = []
        with pytest.raises(BundleError):
            for line, record in bundle.iter_records(tb.NOTES):
                seen.append((line, record))
        assert [line for line, _ in seen] == [1]


@pytest.mark.parametrize(
    "line",
    [
        b'{"uid": "\xff", "display_name": "x"}\n',
        b"[" * 5000 + b"]" * 5000 + b"\n",
        b'{"uid": "u", "display_name": "x"} trailing\n',
        b"null\n",
    ],
)
def test_unparseable_lines(tmp_path: Path, line: bytes) -> None:
    data = raw_members(actors=line)
    assert "actors.jsonl line 1" in iter_refused(
        build(tmp_path / "b.zip", data), tb.ACTORS
    )


def chat_data(message: dict) -> dict[str, bytes]:
    session = json.loads(members(chats=True)[tb.CHAT_SESSIONS][0].model_dump_json())
    return raw_members(
        **{tb.CHAT_SESSIONS: jsonl(session), tb.CHAT_MESSAGES: jsonl(message)}
    )


BASE_MESSAGE = {
    "id": 1,
    "session_id": 7,
    "role": "assistant",
    "content": "x",
    "created_at": "2026-10-01T10:00:00Z",
}


@pytest.mark.parametrize(
    "fields",
    [
        '"replay": {"reasoning_content": "r", "content": [{"type": "tool_use"}]}',
        '"replay": {"content": "x"}',
        '"replay": "reasoning"',
        '"tool_calls": [{"id": "c", "name": "n", "arguments": "{}"}]',
        '"tool_calls": [{"id": "c", "name": "n", "arguments": {"a": NaN}}]',
        '"tool_calls": [{"id": "c", "name": "n", "arguments": {"a": 1e999}}]',
        '"tool_calls": "[]"',
        '"is_error": 0',
    ],
)
def test_chat_message_refusals(tmp_path: Path, fields: str) -> None:
    line = json.dumps(BASE_MESSAGE)[:-1] + ", " + fields + "}\n"
    data = chat_data(BASE_MESSAGE)
    data[tb.CHAT_MESSAGES] = line.encode()
    path = build(tmp_path / "b.zip", data)
    assert "chat_messages.jsonl line 1" in iter_refused(path, tb.CHAT_MESSAGES)


def test_chat_message_typed_columns_parse(tmp_path: Path) -> None:
    message = {
        **BASE_MESSAGE,
        "tool_calls": [
            {"id": "c", "name": "n", "arguments": {"q": {"deep": [1, None]}}}
        ],
        "replay": {"reasoning_content": "r"},
        "grounding": '[{"kind": "path", "path": "src/a.py"}]',
        "rating": 1,
    }
    with read_bundle(build(tmp_path / "b.zip", chat_data(message))) as bundle:
        [(_, got)] = list(bundle.iter_records(tb.CHAT_MESSAGES))
    assert got.tool_calls[0].arguments == {"q": {"deep": [1, None]}}
    assert got.replay is not None and got.replay.reasoning_content == "r"
    assert not hasattr(got, "rating")


def test_origin_key_must_be_a_sha256(tmp_path: Path) -> None:
    session = json.loads(members(chats=True)[tb.CHAT_SESSIONS][0].model_dump_json())
    session["origin_key"] = "short"
    data = raw_members(**{tb.CHAT_SESSIONS: jsonl(session), tb.CHAT_MESSAGES: b""})
    iter_refused(build(tmp_path / "b.zip", data), tb.CHAT_SESSIONS)


def test_unexpected_exceptions_map_to_bundle_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = written(tmp_path)

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("password required")

    monkeypatch.setattr(zipfile.ZipFile, "open", broken)
    assert "password" not in str(refused(path))


# --- D7: the column inventory -------------------------------------------------------

EXPORTED: dict[str, tuple[type, type]] = {
    "project_note": (NoteRecord, db_models.ProjectNote),
    "project_purpose_run": (PurposeRunRecord, db_models.ProjectPurposeRun),
    "rationale_cache": (RationaleRecord, db_models.RationaleCache),
    "commit": (CommitDescriptionRecord, db_models.Commit),
    "commit_file_change": (FileDescriptionRecord, db_models.CommitFileChange),
    "chat_session": (ChatSessionRecord, db_models.ChatSession),
    "chat_message": (ChatMessageRecord, db_models.ChatMessage),
}
"""Each exported table and its wire model."""

DERIVED: dict[str, set[str]] = {
    "project_note": {"edited_on_confirm"},
    "chat_session": {"origin_key"},
}
"""Wire-only fields with no column of their own."""

_SCAN = "rebuilt by the target's scan from git"

NOT_EXPORTED: dict[str, dict[str, str]] = {
    "project_note": {
        "proposed_body": "only ever read as a flag; travels as edited_on_confirm (F15)",
    },
    "project_purpose_run": {
        "id": "a new id on the target; nothing references a run",
    },
    "rationale_cache": {
        "import_id": "target-side provenance, set by the importer (Q6)",
    },
    "commit": {
        "parent_shas": _SCAN,
        "author_name": _SCAN,
        "author_email": _SCAN,
        "authored_at": _SCAN,
        "committed_at": _SCAN,
        "subject": _SCAN,
        "body": _SCAN,
        "files_changed": _SCAN,
        "insertions": _SCAN,
        "deletions": _SCAN,
        "scanned_at": "the target's own scan time",
        "refactor_score": _SCAN,
        "on_default_branch": "computed per checkout by the target's scan",
        "first_seen_ref": "computed per checkout by the target's scan",
    },
    "commit_file_change": {
        "id": "a new id on the target; rows are keyed by (commit_sha, path)",
        "change_type": _SCAN,
        "renamed_from": _SCAN,
        "similarity": _SCAN,
        "lines_added": _SCAN,
        "lines_deleted": _SCAN,
    },
    "chat_session": {
        "owner_uid": "the importer becomes the owner (plan 42 section 5.4)",
    },
    "chat_message": {
        "rating": "rates the source's model output, not knowledge (D-m6)",
        "rating_reason": "travels with rating, which does not (D-m6)",
        "rating_comment": "travels with rating, which does not (D-m6)",
        "rated_at": "travels with rating, which does not (D-m6)",
    },
}
"""Every column of an exported table that the bundle leaves out, with why."""

NOT_TRANSFERRED: dict[str, str] = {
    "author": _SCAN,
    "tag": _SCAN,
    "pull_request": "rebuilt by the target's forge crawl",
    "issue": "rebuilt by the target's forge crawl",
    "milestone": "rebuilt by the target's forge crawl",
    "pr_issue_link": "rebuilt by the target's forge crawl",
    "pr_origin_skip": "the target's own crawl bookkeeping",
    "forge_crawl_state": "the target's own crawl bookkeeping",
    "project_import": "target-side import log",
    "imported_actor": "target-side; built from actors.jsonl",
    "imported_chat": "target-side; built from origin_key",
    "core_state": "recomputed by the next scan",
    "core_file": "recomputed by the next scan",
    "core_rename": "recomputed by the next scan",
    "core_commit": "recomputed by the next scan",
    "core_bulk_pair": "recomputed by the next scan",
}
"""Tables that never travel, with why."""


@pytest.mark.parametrize("table", sorted(EXPORTED))
def test_column_inventory(table: str) -> None:
    wire, model = EXPORTED[table]
    columns = {column.name for column in model.__table__.columns}
    fields = set(wire.model_fields)
    assert DERIVED.get(table, set()) <= fields
    assert not set(NOT_EXPORTED.get(table, {})) & fields
    assert columns == (fields - DERIVED.get(table, set())) | set(
        NOT_EXPORTED.get(table, {})
    )
    assert all(reason.strip() for reason in NOT_EXPORTED.get(table, {}).values())


def test_every_table_is_exported_or_named() -> None:
    tables = set(metadata.tables)
    assert tables == set(EXPORTED) | set(NOT_TRANSFERRED)


def test_every_wire_model_is_inventoried() -> None:
    inventoried = {wire for wire, _ in EXPORTED.values()}
    assert set(tb.MEMBER_MODELS.values()) - inventoried == {ActorRecord}
