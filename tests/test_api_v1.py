"""The ``/api/v1`` wire models (:mod:`whygraph.api_v1`).

Request models validate everything that reaches ``git`` (plan §4.1);
response models mirror the MCP tools' dict shapes exactly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from whygraph.analyze import CommitEvidence, Rationale
from whygraph.api_v1 import (
    MAX_HUNKS,
    MAX_ORIGINS,
    ErrorOut,
    EvidenceIn,
    EvidenceOut,
    HunkIn,
    OriginIn,
    RationaleIn,
    RationaleOut,
    StatusOut,
    TargetIn,
    TargetOut,
    blame_hunks_from_in,
    hunk_in_from_blame,
)
from whygraph.db.models import Commit, Issue, PullRequest
from whygraph.mcp.evidence import _evidence_dict
from whygraph.mcp.rationale import _format_response
from whygraph.mcp.targets import Target, target_dict
from whygraph.services.git import BlameHunk, BlameOrigin

SHA = "a" * 40


def _hunk(**over) -> dict:
    hunk = {"sha": SHA, "origins": [{"path": "src/a.py", "start": 1, "end": 2}]}
    hunk.update(over)
    return hunk


def _origin(**over) -> dict:
    origin = {"path": "src/a.py", "start": 1, "end": 2}
    origin.update(over)
    return origin


def _target(**over) -> dict:
    target = {"path": "src/a.py", "line_start": 1, "line_end": 5}
    target.update(over)
    return target


# ---- requests -------------------------------------------------------------


def test_api_v1_accepts_a_valid_evidence_request() -> None:
    body = EvidenceIn.model_validate(
        {
            "target": _target(qualified_name="pkg.mod.func"),
            "hunks": [_hunk(), _hunk(sha="b" * 64)],
            "limit": 50,
        }
    )
    assert body.limit == 50
    assert body.hunks[1].sha == "b" * 64
    assert EvidenceIn.model_validate({"target": _target()}).limit == 20
    assert (
        RationaleIn.model_validate_json(
            '{"target": {"path": "a.py", "line_start": 3, "line_end": 3}, "hunks": []}'
        ).target.line_start
        == 3
    )


@pytest.mark.parametrize(
    "sha",
    [
        "--output=/tmp/x" + "a" * 25,
        "-" + "a" * 39,
        "0" * 40,
        "0" * 64,
        "A" * 40,
        "a" * 39,
        "a" * 41,
        "a" * 40 + "\n",
        "g" * 40,
        "HEAD",
        "a" * 20 + "^{commit}" + "a" * 11,
    ],
)
def test_api_v1_rejects_option_like_sha(sha: str) -> None:
    with pytest.raises(ValidationError):
        HunkIn.model_validate(_hunk(sha=sha))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "-L1,2",
        "--output=x",
        ":(top)a.py",
        "../a.py",
        "src/../../a.py",
        "src/..",
        "a\x00.py",
        "a\n.py",
        "a\r.py",
        "a" * 4097,
    ],
)
def test_api_v1_rejects_option_like_path(path: str) -> None:
    with pytest.raises(ValidationError):
        OriginIn.model_validate(_origin(path=path))
    with pytest.raises(ValidationError):
        TargetIn.model_validate(_target(path=path))


def test_api_v1_accepts_unusual_but_safe_paths() -> None:
    for path in ("a.py", "src/a-b/c..d.py", "dir/é.py", "a" * 4096, "x/-y.py"):
        assert OriginIn.model_validate(_origin(path=path)).path == path


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 1), (2, 1), (1, 1_000_001), (-1, 3), ("1", 2), (True, 2), (1.0, 2)],
)
def test_api_v1_rejects_bad_ranges(start: object, end: object) -> None:
    with pytest.raises(ValidationError):
        OriginIn.model_validate(_origin(start=start, end=end))
    with pytest.raises(ValidationError):
        TargetIn.model_validate(_target(line_start=start, line_end=end))


def test_api_v1_range_bounds_are_inclusive() -> None:
    assert OriginIn.model_validate(_origin(start=1, end=1_000_000)).end == 1_000_000
    assert OriginIn.model_validate(_origin(start=7, end=7)).start == 7


def test_api_v1_caps_hunks_origins_and_limit() -> None:
    origins = [_origin()] * MAX_ORIGINS
    assert len(HunkIn.model_validate(_hunk(origins=origins)).origins) == MAX_ORIGINS
    with pytest.raises(ValidationError):
        HunkIn.model_validate(_hunk(origins=origins + [_origin()]))
    hunks = [_hunk()] * MAX_HUNKS
    assert (
        len(EvidenceIn.model_validate({"target": _target(), "hunks": hunks}).hunks)
        == MAX_HUNKS
    )
    with pytest.raises(ValidationError):
        EvidenceIn.model_validate({"target": _target(), "hunks": hunks + [_hunk()]})
    with pytest.raises(ValidationError):
        RationaleIn.model_validate({"target": _target(), "hunks": hunks + [_hunk()]})
    for limit in (0, 51, "5", True):
        with pytest.raises(ValidationError):
            EvidenceIn.model_validate({"target": _target(), "limit": limit})


@pytest.mark.parametrize("name", ["", "a" * 1025, "pkg.\x1bfunc", "pkg.func\n", 5])
def test_api_v1_rejects_bad_qualified_name(name: object) -> None:
    with pytest.raises(ValidationError):
        TargetIn.model_validate(_target(qualified_name=name))


def test_api_v1_refuses_unknown_request_fields() -> None:
    with pytest.raises(ValidationError):
        HunkIn.model_validate(_hunk(rev="HEAD"))
    with pytest.raises(ValidationError):
        EvidenceIn.model_validate({"target": _target(), "ignore_revs_file": "/etc"})


# ---- responses ------------------------------------------------------------


def _commit() -> Commit:
    return Commit(
        sha=SHA,
        parent_shas="",
        author_name="Test User",
        author_email="tester@example.com",
        authored_at="2026-01-01T00:00:00+00:00",
        committed_at="2026-01-01T00:00:00+00:00",
        subject="a change",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-05-01T00:00:00+00:00",
    )


def _evidence() -> CommitEvidence:
    pr = PullRequest(
        number=5,
        title="A pull request",
        body=None,
        state="merged",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        merged_at="2026-02-01T00:00:00+00:00",
        merge_commit_sha=SHA,
        head_sha="headsha",
        base_ref="main",
        author=None,
        html_url="https://example.com/pr/5",
        labels='["enhancement"]',
        commit_titles='[{"oid": "abc", "headline": "first"}]',
        comments='[{"author": "alice", "body": "lgtm"}]',
        fetched_at="2026-02-02T00:00:00+00:00",
    )
    issue = Issue(
        number=9,
        title="An issue",
        body="Issue body.",
        state="closed",
        created_at="2025-12-01T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
        author="reporter",
        html_url="https://example.com/issue/9",
        labels='["bug"]',
        fetched_at="2026-02-02T00:00:00+00:00",
    )
    return CommitEvidence(_commit(), (pr,), (issue,), source="pr-origin")


def test_evidence_out_mirrors_evidence_dict_exactly() -> None:
    payload = _evidence_dict(_evidence())
    model = EvidenceOut.model_validate(payload)
    assert model.push_status is None
    # push_status is omitted when unset, so the round trip is exact.
    assert model.model_dump() == payload
    assert list(model.model_dump()) == list(payload)
    assert list(model.model_dump()["commit"]) == list(payload["commit"])
    labelled = EvidenceOut.model_validate({**payload, "push_status": "not_pushed"})
    assert labelled.model_dump()["push_status"] == "not_pushed"
    with pytest.raises(ValidationError):
        EvidenceOut.model_validate({**payload, "source": "guess"})
    with pytest.raises(ValidationError):
        EvidenceOut.model_validate({**payload, "push_status": "lost"})


def test_rationale_out_mirrors_the_tool_response() -> None:
    rationale = Rationale(
        purpose="p",
        why="w",
        constraints=("c",),
        tradeoffs=(),
        risks=("r1", "r2"),
        model="m",
        provider="stub",
    )
    for target in (Target("a.py", 1, 2, None), Target("a.py", 1, 2, "pkg.f", True)):
        payload = _format_response(
            target, rationale, [_evidence()], "2026-01-01T00:00:00+00:00"
        )
        assert RationaleOut.model_validate(payload).model_dump() == payload
        assert TargetOut.model_validate(
            target_dict(target)
        ).model_dump() == target_dict(target)


def test_status_and_error_out() -> None:
    status = StatusOut.model_validate(
        {
            "slug": "demo",
            "name": "Demo",
            "github_full_name": "acme/demo",
            "clone_url": "https://github.com/acme/demo.git",
            "default_branch": "main",
            "last_scanned_head": SHA,
            "last_scan_at": "2026-01-01T00:00:00+00:00",
            "access_lost": False,
            "role": "member",
            "added_later": True,
        }
    )
    assert status.role == "member"
    with pytest.raises(ValidationError):
        StatusOut.model_validate({"slug": "s", "name": "n", "role": "reader"})
    error = ErrorOut.model_validate(
        {"error": "nope", "code": "token_revoked", "reason": "left"}
    )
    assert error.retry_after is None
    with pytest.raises(ValidationError):
        ErrorOut.model_validate({"error": "nope"})


# ---- conversions ----------------------------------------------------------


def test_hunk_conversions_round_trip() -> None:
    hunk = BlameHunk(
        SHA,
        3,
        "A",
        "a@example.com",
        "s",
        None,
        (BlameOrigin("old.py", 4, 5, 10), BlameOrigin("x.py", 1, 1, 2)),
    )
    wire = hunk_in_from_blame(hunk)
    assert wire.model_dump() == {
        "sha": SHA,
        "origins": [
            {"path": "old.py", "start": 4, "end": 5},
            {"path": "x.py", "start": 1, "end": 1},
        ],
    }
    (back,) = blame_hunks_from_in([wire])
    assert back.sha == SHA
    assert back.lines_owned == 3
    assert [(o.path, o.start, o.end) for o in back.origins] == [
        ("old.py", 4, 5),
        ("x.py", 1, 1),
    ]
    # final_start keeps the request order.
    assert [o.final_start for o in back.origins] == [1, 2]
    with pytest.raises(ValidationError):
        hunk_in_from_blame(BlameHunk("0" * 40, 1, None, None, None, None))
