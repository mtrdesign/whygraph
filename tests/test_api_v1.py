"""The ``/api/v1`` wire models (:mod:`whygraph.api_v1`).

Request models validate everything that reaches ``git`` (plan §4.1);
response models mirror the MCP tools' dict shapes exactly.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from whygraph.analyze import CommitEvidence, Item, ItemRef, Rationale
from whygraph.api_v1 import (
    API_VERSION,
    MAX_HUNKS,
    MAX_ORIGINS,
    ErrorOut,
    EvidenceIn,
    EvidenceOut,
    HunkIn,
    OriginIn,
    RationaleIn,
    RationaleItemOut,
    RationaleOut,
    StatusOut,
    TargetIn,
    TargetOut,
    blame_hunks_from_in,
    hunk_in_from_blame,
)
from whygraph.db.models import Commit, Issue, PullRequest
from whygraph.mcp.evidence import ReviewScope, _evidence_dict, evidence_dicts
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


def _discussed_evidence() -> CommitEvidence:
    """``_evidence()`` after a v2 crawl: summaries, threads, comments, totals."""
    item = _evidence()
    pr, issue = item.pull_requests[0], item.issues[0]
    pr.review_summaries = json.dumps(
        [{"author": "omar", "state": "APPROVED", "body": "ok", "submitted_at": "t"}]
    )
    pr.review_threads = json.dumps(
        [
            {
                "path": "src/a.py",
                "original_line": 3,
                "anchor": {"lines": ["return compute_the_value(x)"], "added": True},
                "resolved": False,
                "outdated": False,
                "comments": [
                    {"author": "bot", "body": "q", "bot": True, "minimized": False},
                    {"author": "jane", "body": "because", "created_at": "t"},
                ],
                "comments_total": 2,
            },
            {"path": "b.py", "original_line": None, "anchor": None, "comments": []},
        ]
    )
    pr.review_threads_total = 4
    pr.comments_total = 30
    issue.comments = json.dumps([{"author": "a", "body": "hi", "created_at": "t"}])
    issue.comments_total = 5
    return item


def test_evidence_out_round_trips_the_review_fields_exactly() -> None:
    scope = ReviewScope(frozenset({"src/a.py"}), ("return compute_the_value(x)",))
    payload = _evidence_dict(_discussed_evidence(), scope)
    model = EvidenceOut.model_validate(payload)
    assert model.model_dump() == payload
    assert list(model.model_dump()["pull_requests"][0]) == list(
        payload["pull_requests"][0]
    )
    assert list(model.model_dump()["issues"][0]) == list(payload["issues"][0])
    pr = model.pull_requests[0]
    assert pr.review_threads[0]["current"] is True
    assert (pr.review_threads_elsewhere, pr.review_threads_total) == (1, 4)
    assert model.issues[0].comments_total == 5


def test_an_old_shape_answer_parses_with_the_new_models() -> None:
    """An older platform sends no review keys: they default, never ``None``."""
    payload = _evidence_dict(_evidence())
    for key in (
        "review_summaries",
        "review_threads",
        "review_threads_elsewhere",
        "review_threads_total",
    ):
        del payload["pull_requests"][0][key]
    del payload["issues"][0]["comments"], payload["issues"][0]["comments_total"]
    model = EvidenceOut.model_validate(payload)
    pr, issue = model.pull_requests[0], model.issues[0]
    assert pr.review_summaries == [] and pr.review_threads == []
    assert pr.review_threads_elsewhere == 0 and pr.review_threads_total == 0
    assert issue.comments == [] and issue.comments_total == 0


def test_a_new_shape_answer_parses_with_the_old_models() -> None:
    """A client built before plan 62 ignores the new keys (no API bump, R5)."""
    from typing import Any

    from pydantic import BaseModel, ConfigDict, Field

    class _OldOut(BaseModel):
        model_config = ConfigDict(extra="ignore")

    class OldPullRequestOut(_OldOut):
        number: int
        title: str
        body: str | None = None
        state: str
        merged_at: str | None = None
        author: str | None = None
        html_url: str
        labels: list[Any] = Field(default_factory=list)
        commit_titles: list[Any] = Field(default_factory=list)
        comments: list[Any] = Field(default_factory=list)

    class OldIssueOut(_OldOut):
        number: int
        title: str
        body: str | None = None
        state: str
        author: str | None = None
        html_url: str
        labels: list[Any] = Field(default_factory=list)

    class OldEvidenceOut(_OldOut):
        commit: dict
        pull_requests: list[OldPullRequestOut] = Field(default_factory=list)
        issues: list[OldIssueOut] = Field(default_factory=list)
        source: str

    scope = ReviewScope(frozenset({"src/a.py"}), ())
    payload = _evidence_dict(_discussed_evidence(), scope)
    old = OldEvidenceOut.model_validate(payload)
    assert old.pull_requests[0].comments == payload["pull_requests"][0]["comments"]
    assert old.issues[0].labels == ["bug"]


def test_a_fifty_item_evidence_reply_at_every_bound_stays_small() -> None:
    """Acceptance 9: 50 distinct PRs at every per-PR bound fit in a quarter cap."""
    from whygraph.api_v1 import EvidenceReplyOut
    from whygraph.portal.platform_client import MAX_RESPONSE_BYTES

    def comments(count: int) -> list[dict]:
        return [
            {
                "author": f"u{n}",
                "body": "x" * 5000,
                "created_at": "2026-01-01T00:00:00Z",
            }
            for n in range(count)
        ]

    thread = {
        "path": "src/a.py",
        "original_line": 3,
        "anchor": {"lines": ["return compute_the_value(x)"], "added": True},
        "resolved": False,
        "outdated": False,
        "comments": comments(10),
        "comments_total": 14,
    }
    items = []
    for n in range(50):
        pr = PullRequest(
            number=100 + n,
            title=f"PR {n}",
            state="merged",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-02-01T00:00:00+00:00",
            html_url=f"https://example.com/pr/{n}",
            fetched_at="2026-02-02T00:00:00+00:00",
            review_summaries=json.dumps(
                [{**c, "state": "COMMENTED"} for c in comments(60)]
            ),
            review_threads=json.dumps([thread] * 20),
            review_threads_total=40,
        )
        issue = Issue(
            number=500 + n,
            title=f"Issue {n}",
            state="closed",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-02-01T00:00:00+00:00",
            html_url=f"https://example.com/issue/{n}",
            fetched_at="2026-02-02T00:00:00+00:00",
            comments=json.dumps(comments(100)),
            comments_total=100,
        )
        commit = _commit()
        commit.sha = f"{n:040x}"
        items.append(CommitEvidence(commit, (pr,), (issue,), source="blame"))
    scope = ReviewScope(frozenset({"src/a.py"}), ("return compute_the_value(x)",))

    reply = EvidenceReplyOut.model_validate(
        {"evidence": evidence_dicts(items, scope), "unknown_shas": []}
    )
    size = len(json.dumps(reply.model_dump(mode="json")).encode())

    assert size < MAX_RESPONSE_BYTES // 4
    last = reply.evidence[-1].pull_requests[0]
    assert last.review_threads == [] and last.review_threads_total == 40


ITEMS = (
    Item(
        "keep it small",
        "quoted",
        (ItemRef("commit", SHA), ItemRef("pr", "7", "https://github.com/o/r/pull/7")),
        quote="keep it small",
    ),
    Item("callers pass bytes", "cited", (ItemRef("issue", "3"),)),
    Item("no network here", "inferred"),
    Item("an old string", "unknown"),
)
"""One item of each basis, with and without a ``url`` and a ``quote``."""


def test_rationale_out_mirrors_the_tool_response() -> None:
    rationale = Rationale(
        purpose="p",
        why="w",
        constraints=ITEMS,
        tradeoffs=("t",),
        risks=ITEMS[1:3],
        model="m",
        provider="stub",
    )
    for target in (Target("a.py", 1, 2, None), Target("a.py", 1, 2, "pkg.f", True)):
        payload = _format_response(
            target, rationale, [_evidence()], "2026-01-01T00:00:00+00:00"
        )
        model = RationaleOut.model_validate(payload)
        assert model.model_dump() == payload
        assert model.model_dump(mode="json") == payload
        assert isinstance(model.constraints[0], RationaleItemOut)
        assert TargetOut.model_validate(
            target_dict(target)
        ).model_dump() == target_dict(target)
    # `url` and `quote` are left out when unset, as `Item.as_dict` leaves them.
    first, second = payload["constraints"][:2]
    assert first["refs"][1]["url"] and "url" not in first["refs"][0]
    assert "quote" in first and "quote" not in second


def test_rationale_item_out_refuses_an_unknown_basis_or_ref_kind() -> None:
    item = {"text": "x", "basis": "quoted", "refs": [{"kind": "pr", "id": "1"}]}
    assert RationaleItemOut.model_validate(item).model_dump() == item
    with pytest.raises(ValidationError):
        RationaleItemOut.model_validate({**item, "basis": "guessed"})
    with pytest.raises(ValidationError):
        RationaleItemOut.model_validate({**item, "refs": [{"kind": "tag", "id": "1"}]})
    # A version-1 card (plain strings) does not parse: `/meta` stops that
    # client first (`API_VERSION`), so it never gets this far.
    card = _format_response(
        Target("a.py", 1, 2, None),
        Rationale("p", "w", (), (), (), "m", "stub"),
        [_evidence()],
        "2026-01-01T00:00:00+00:00",
    )
    RationaleOut.model_validate(card)
    with pytest.raises(ValidationError):
        RationaleOut.model_validate({**card, "constraints": ["a plain string"]})


def test_rationale_out_mirrors_a_card_with_depth_coverage_deeper_and_checks() -> None:
    """Plan 62 section 14.4: the additive card fields round-trip unchanged."""
    from dataclasses import replace

    from whygraph.analyze.card_bundle import CardCoverage
    from whygraph.analyze.rationale import ItemCheck

    checked = replace(ITEMS[0], check=ItemCheck("holds", "line 2 still does it"))
    rationale = Rationale(
        purpose="p",
        why="w",
        constraints=(checked, ITEMS[2]),
        tradeoffs=(),
        risks=(ITEMS[1],),
        model="m",
        provider="stub",
        coverage=CardCoverage(
            line_commits=1, trimmed=("full_pr_budget",), verification="verified"
        ),
    )
    payload = {
        **_format_response(
            Target("a.py", 1, 2, None),
            rationale,
            [_evidence()],
            "2026-01-01T00:00:00+00:00",
        ),
        "depth": {"requested": "auto", "resolved": "high", "reason": "r"},
        "deeper": {"level": "xhigh", "adds": "More."},
    }
    assert payload["constraints"][0]["check"]["verdict"] == "holds"
    model = RationaleOut.model_validate(payload)
    assert model.model_dump(mode="json") == payload
    # None is left out, as an older platform's card never had them.
    bare = RationaleOut.model_validate({**payload, "deeper": None, "coverage": None})
    assert {"deeper", "coverage"}.isdisjoint(bare.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        RationaleItemOut.model_validate(
            {"text": "x", "basis": "inferred", "check": {"verdict": "maybe"}}
        )


def test_rationale_out_is_a_card_or_a_not_cached_miss() -> None:
    target = {"path": "a.py", "line_start": 1, "line_end": 2}
    miss = RationaleOut.model_validate({"status": "not_cached", "target": target})
    assert miss.status == "not_cached" and miss.purpose is None
    with pytest.raises(ValidationError, match="a card needs purpose"):
        RationaleOut.model_validate({"target": target, "why": "w"})


def test_rationale_in_depth_fields() -> None:
    """Optional, strict, left out of the body when unset (an older platform refuses them)."""
    from typing import get_args

    from whygraph.api_v1 import DepthLevel, Intent
    from whygraph.core.config import OFFERED_DEPTH_LEVELS
    from whygraph.mcp.rationale_depth import INTENTS

    assert get_args(Intent) == INTENTS
    assert get_args(DepthLevel) == OFFERED_DEPTH_LEVELS
    bare = RationaleIn(target=_target())
    assert set(bare.model_dump(mode="json")) == {"target", "hunks"}
    full = RationaleIn(
        target=_target(), intent="review", depth="low", cached_only=False
    )
    assert full.model_dump(mode="json")["cached_only"] is False
    for bad in (
        {"depth": "xhigh"},  # not offered until Part D: refused
        {"intent": "guess"},
        {"cached_only": "true"},
        {"cached_only": 1},
    ):
        with pytest.raises(ValidationError):
            RationaleIn.model_validate({"target": _target(), **bad})


def test_api_version_is_2() -> None:
    """Plan 54 D5: card items became objects, so the contract version moved."""
    assert API_VERSION == 2


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
            "project_role": "viewer",
            "added_later": True,
        }
    )
    assert (status.role, status.project_role) == ("member", "viewer")
    for bad in (
        {"role": "reader", "project_role": "viewer"},
        {"role": "member", "project_role": "owner"},
        {"role": "member"},
    ):
        with pytest.raises(ValidationError):
            StatusOut.model_validate({"slug": "s", "name": "n", **bad})
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
