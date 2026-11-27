"""Incremental iteration in the GitHub service (incremental-scan plan, step 1)."""

from __future__ import annotations

import json
import logging
import re

import pytest
from test_services_github_token import RecordingShell

from whygraph.core import ShellError
from whygraph.services.github import GitHubError
from whygraph.services.github import issues as issues_mod
from whygraph.services.github import pull_requests as prs_mod
from whygraph.services.github.commands import (
    LOOKBACK,
    floor_for,
    format_github_ts,
    parse_github_ts,
)
from whygraph.services.github.issue import Issue
from whygraph.services.github.issues import Issues
from whygraph.services.github.pull_request import PullRequest, ReviewThread
from whygraph.services.github.pull_requests import DETAILS_BATCH, PullRequests

MARK = "2026-10-26T12:00:00Z"


class CmdShell(RecordingShell):
    """``RecordingShell`` that also records each command it was asked to run."""

    def __init__(self, responses: list) -> None:
        super().__init__(responses)
        self.cmds: list = []

    def run(self, cmd, *, env=None, **kwargs):  # noqa: ANN001, ANN201
        self.cmds.append(cmd)
        return super().run(cmd, env=env, **kwargs)

    def args(self, index: int) -> dict[str, str]:
        argv = self.cmds[index].argv()
        pairs = [a for a in argv if "=" in a and not a.startswith("query=")]
        return dict(p.split("=", 1) for p in pairs)

    def query(self, index: int) -> str:
        return next(
            a for a in self.cmds[index].argv() if a.startswith("query=")
        ).removeprefix("query=")


def _node(number: int, updated: str = MARK) -> dict:
    return {
        "number": number,
        "state": "OPEN",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": updated,
        "url": f"https://example.test/{number}",
    }


def _page(path: str, nodes: list[dict], *, more: bool = False) -> dict:
    return {
        "repository": {
            path: {
                "pageInfo": {"hasNextPage": more, "endCursor": "c1" if more else None},
                "nodes": nodes,
            }
        }
    }


def test_updates_since_stops_at_the_floor_and_requests_no_further_page() -> None:
    nodes = [
        {"number": 9, "updatedAt": "2026-10-26T12:30:00Z"},
        {"number": 8, "updatedAt": "2026-10-26T11:55:00Z"},  # inside the lookback
        {"number": 7, "updatedAt": "2026-10-26T11:49:59Z"},  # older than the floor
        {"number": 6, "updatedAt": "2026-10-26T11:00:00Z"},
    ]
    shell = CmdShell([_page("pullRequests", nodes, more=True)])
    got = list(PullRequests("o", "r", shell=shell).updates_since(MARK))
    assert got == [(9, "2026-10-26T12:30:00Z"), (8, "2026-10-26T11:55:00Z")]
    assert len(shell.cmds) == 1


def test_updates_query_is_light_and_newest_first() -> None:
    q = prs_mod._UPDATES_QUERY
    assert "nodes { number updatedAt }" in q
    assert "UPDATED_AT, direction: DESC" in q
    assert "title" not in q and "commits" not in q


def test_by_numbers_batches_and_declares_only_owner_and_name() -> None:
    assert list(PullRequests("o", "r", shell=CmdShell([])).by_numbers([])) == []
    numbers = list(range(1, 46))
    shell = CmdShell([])
    shell.responses = [
        {"repository": {f"pr{n}": _node(n) for n in numbers[i : i + DETAILS_BATCH]}}
        for i in range(0, len(numbers), DETAILS_BATCH)
    ]
    got = list(PullRequests("o", "r", shell=shell).by_numbers(numbers))
    assert len(shell.cmds) == 3
    assert DETAILS_BATCH == 20
    sizes = [
        len(re.findall(r"pr\d+: pullRequest\(number: \d+\)", shell.query(i)))
        for i in range(3)
    ]
    assert sizes == [20, 20, 5]
    for i in range(3):
        q = shell.query(i)
        assert "$cursor" not in q
        assert q.startswith("query($owner: String!, $name: String!) {")
    assert "pr1: pullRequest(number: 1)" in shell.query(0)
    assert "pr45: pullRequest(number: 45)" in shell.query(2)
    assert got == [PullRequest.from_graphql_node(_node(n)) for n in numbers]


def test_by_numbers_skips_a_null_alias_and_wraps_shell_errors() -> None:
    shell = CmdShell([{"repository": {"pr1": _node(1), "pr2": None, "pr3": _node(3)}}])
    got = [
        pr.number for pr in PullRequests("o", "r", shell=shell).by_numbers([1, 2, 3])
    ]
    assert got == [1, 3]

    class Failing(CmdShell):
        def run(self, cmd, *, env=None, **kwargs):  # noqa: ANN001, ANN201
            raise ShellError(["gh"], 1, "", "Could not resolve to a PullRequest")

    with pytest.raises(GitHubError, match="Could not resolve"):
        list(PullRequests("o", "r", shell=Failing([])).by_numbers([1]))


def test_list_queries_share_pr_fields_and_walk_newest_first() -> None:
    fields = prs_mod._PR_FIELDS
    assert fields in prs_mod._list_query(100)
    shell = CmdShell([{"repository": {"pr7": _node(7)}}])
    list(PullRequests("o", "r", shell=shell).by_numbers([7]))
    assert fields in shell.query(0)
    for needle in (
        "closingIssuesReferences(first: 50)",
        "commits(first: 250)",
        "comments(first: 100)",
        "labels(first: 20)",
        "mergeCommit { oid }",
    ):
        assert needle in prs_mod._list_query(100)
    for q in (prs_mod._list_query(100), issues_mod._list_query(100)):
        assert "UPDATED_AT, direction: DESC" in q
        assert "CREATED_AT" not in q


def test_issues_updated_since_sends_the_floor_and_follows_every_page() -> None:
    shell = CmdShell(
        [
            _page("issues", [_node(2, "2026-10-26T12:30:00Z")], more=True),
            _page("issues", [_node(1, "2000-01-01T00:00:00Z")]),
        ]
    )
    got = [i.number for i in Issues("o", "r", shell=shell).updated_since(MARK)]
    assert got == [2, 1]  # no client-side stop
    assert len(shell.cmds) == 2
    assert shell.args(0)["since"] == "2026-10-26T11:50:00Z"
    assert shell.args(1)["since"] == "2026-10-26T11:50:00Z"
    assert "filterBy: {since: $since}" in shell.query(0)
    assert "$since: DateTime!" in shell.query(0)
    assert format_github_ts(floor_for(MARK)) == "2026-10-26T11:50:00Z"


def test_timestamp_helpers_round_trip() -> None:
    parsed = parse_github_ts(MARK)
    assert parsed.utcoffset() is not None
    assert format_github_ts(parsed) == MARK
    assert parsed - floor_for(MARK) == LOOKBACK


class _Flaky(CmdShell):
    """``CmdShell`` where an ``Exception`` response is raised, not returned."""

    def run(self, cmd, *, env=None, **kwargs):  # noqa: ANN001, ANN201
        self.cmds.append(cmd)
        response = self.responses[0]
        if isinstance(response, Exception):
            self.responses.pop(0)
            raise response
        return super(CmdShell, self).run(cmd, env=env, **kwargs)


def _size(query: str) -> int:
    return int(re.search(r"pullRequests\(\s*first: (\d+)", query).group(1))


def _gateway(code: int = 502) -> ShellError:
    return ShellError(["gh"], 1, "", f"gh: Something went wrong (HTTP {code})")


def test_pull_request_list_retries_a_502_at_half_the_page_size() -> None:
    page = _page("pullRequests", [_node(1), _node(2)])
    shell = _Flaky([_gateway(), page])
    got = [pr.number for pr in PullRequests("o", "r", shell=shell)]
    assert got == [1, 2]
    assert _size(shell.query(0)) == 50
    assert _size(shell.query(1)) == 25


def test_pull_request_list_keeps_the_reduced_size_for_the_next_page() -> None:
    first = _page("pullRequests", [_node(1)], more=True)
    second = _page("pullRequests", [_node(2)])
    shell = _Flaky([_gateway(504), first, second])
    assert [pr.number for pr in PullRequests("o", "r", shell=shell)] == [1, 2]
    assert _size(shell.query(2)) == 25
    assert shell.args(2)["cursor"] == "c1"


def test_pull_request_list_fails_after_four_gateway_errors() -> None:
    shell = _Flaky([_gateway(), _gateway(), _gateway(), _gateway()])
    with pytest.raises(GitHubError, match="HTTP 502"):
        [*iter(PullRequests("o", "r", shell=shell))]
    assert [_size(shell.query(i)) for i in range(4)] == [50, 25, 10, 5]


def _truncated() -> ShellError:
    return ShellError(["gh"], 1, "", "unexpected end of JSON input")


def test_pull_request_list_retries_a_truncated_answer_at_half_the_page_size() -> None:
    page = _page("pullRequests", [_node(1), _node(2)])
    shell = _Flaky([_truncated(), page])
    assert [pr.number for pr in PullRequests("o", "r", shell=shell)] == [1, 2]
    assert _size(shell.query(0)) == 50
    assert _size(shell.query(1)) == 25


def test_pull_request_list_fails_after_repeated_truncated_answers() -> None:
    shell = _Flaky([_truncated(), _truncated(), _truncated(), _truncated()])
    with pytest.raises(GitHubError, match="unexpected end of JSON input"):
        [*iter(PullRequests("o", "r", shell=shell))]
    assert _size(shell.query(3)) == 5


def test_by_numbers_splits_a_batch_on_a_truncated_answer() -> None:
    nums = list(range(1, DETAILS_BATCH + 1))
    shell = _Flaky(
        [
            _truncated(),
            {"repository": {f"pr{n}": _node(n) for n in nums[:10]}},
            {"repository": {f"pr{n}": _node(n) for n in nums[10:]}},
        ]
    )
    got = [pr.number for pr in PullRequests("o", "r", shell=shell).by_numbers(nums)]
    assert got == nums


def test_pull_request_list_does_not_retry_other_failures() -> None:
    shell = _Flaky([ShellError(["gh"], 1, "", "HTTP 403 forbidden")])
    with pytest.raises(GitHubError, match="403"):
        [*iter(PullRequests("o", "r", shell=shell))]
    assert len(shell.cmds) == 1


def test_by_numbers_splits_a_batch_on_a_gateway_error() -> None:
    nums = list(range(1, DETAILS_BATCH + 1))
    shell = _Flaky(
        [
            _gateway(),
            {"repository": {f"pr{n}": _node(n) for n in nums[:10]}},
            {"repository": {f"pr{n}": _node(n) for n in nums[10:]}},
        ]
    )
    got = [pr.number for pr in PullRequests("o", "r", shell=shell).by_numbers(nums)]
    assert got == nums
    assert "pr10:" in shell.query(1) and "pr11:" not in shell.query(1)
    assert "pr11:" in shell.query(2)


def _forbidden(*paths: list, extra: dict | None = None) -> ShellError:
    errors = [
        {"type": "FORBIDDEN", "path": path, "message": "Resource not accessible"}
        for path in paths
    ]
    errors += [extra] if extra else []
    nodes = [{**_node(1), "reviewRequests": {"nodes": [None]}}, _node(2)]
    out = json.dumps({"data": _page("pullRequests", nodes), "errors": errors})
    return ShellError(["gh"], 1, out, "gh: Resource not accessible")


_RR = ["repository", "pullRequests", "nodes", 0, "reviewRequests", "nodes", 0]


def test_forbidden_review_requests_are_dropped_not_fatal(caplog, monkeypatch) -> None:
    # A neighbour's configure_logging may have set propagate=False; caplog needs it.
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    shell = _Flaky([_forbidden(_RR, _RR)])
    with caplog.at_level("WARNING"):
        got = [*iter(PullRequests("o", "r", shell=shell))]
    assert [pr.number for pr in got] == [1, 2]
    assert got[0].review_requests == ()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "2" in warnings[0].getMessage()
    assert len(shell.cmds) == 1


def test_forbidden_elsewhere_or_mixed_errors_still_fail() -> None:
    elsewhere = _forbidden(["repository", "pullRequests", "nodes", 0, "files"])
    with pytest.raises(GitHubError):
        [*iter(PullRequests("o", "r", shell=_Flaky([elsewhere])))]
    mixed = _forbidden(_RR, extra={"type": "INTERNAL", "path": ["repository"]})
    with pytest.raises(GitHubError):
        [*iter(PullRequests("o", "r", shell=_Flaky([mixed])))]


def test_by_numbers_tolerates_forbidden_review_requests() -> None:
    out = json.dumps(
        {
            "data": {"repository": {"pr1": _node(1)}},
            "errors": [
                {
                    "type": "FORBIDDEN",
                    "path": ["repository", "pr1", "reviewRequests", "nodes", 0],
                    "message": "x",
                }
            ],
        }
    )
    shell = _Flaky([ShellError(["gh"], 1, out, "")])
    assert [p.number for p in PullRequests("o", "r", shell=shell).by_numbers([1])] == [
        1
    ]


def test_issues_never_tolerate_forbidden() -> None:
    shell = _Flaky([_forbidden(_RR)])
    with pytest.raises(GitHubError):
        [*iter(Issues("o", "r", shell=shell))]


def test_pr_parser_reads_the_new_fields() -> None:
    node = {
        **_node(7),
        "author": {"login": "me"},
        "milestone": {"number": 2},
        "assignees": {"nodes": [{"login": "ann"}]},
        "reviewDecision": "CHANGES_REQUESTED",
        "latestOpinionatedReviews": {
            "nodes": [
                {
                    "author": {"login": "bob"},
                    "state": "CHANGES_REQUESTED",
                    "submittedAt": "2026-02-01T00:00:00Z",
                }
            ]
        },
        "reviewRequests": {
            "nodes": [
                {"requestedReviewer": {"login": "cy"}},
                {"requestedReviewer": {}},
                None,
            ]
        },
        "reviews": {
            "nodes": [
                {"author": {"login": "me"}, "submittedAt": "2026-01-02T00:00:00Z"},
                {"author": {"login": "bob"}, "submittedAt": "2026-02-01T00:00:00Z"},
                {"author": {"login": "ann"}, "submittedAt": "2026-01-05T00:00:00Z"},
            ]
        },
        "files": {"nodes": [{"path": "a.py"}]},
        "additions": 3,
        "deletions": 1,
        "changedFiles": 1,
    }
    pr = PullRequest.from_graphql_node(node)
    assert (pr.milestone, pr.assignees, pr.review_decision) == (
        2,
        ("ann",),
        "CHANGES_REQUESTED",
    )
    assert pr.review_requests == ("cy",)
    assert pr.first_review_at == "2026-01-05T00:00:00Z"
    assert pr.files == ("a.py",) and pr.changed_files == 1
    bare = PullRequest.from_graphql_node(_node(8))
    assert bare.first_review_at is None and bare.files == () and bare.milestone is None


def test_pr_fields_have_no_team_fragment() -> None:
    assert "Team" not in prs_mod._PR_FIELDS
    assert "reviewRequests" in prs_mod._PR_FIELDS


def _issue_size(query: str) -> int:
    return int(re.search(r"issues\(\s*first: (\d+)", query).group(1))


def test_issue_list_shrinks_through_issue_page_sizes() -> None:
    shell = _Flaky([_gateway(), _gateway(), _gateway(), _gateway()])
    with pytest.raises(GitHubError, match="HTTP 502"):
        [*iter(Issues("o", "r", shell=shell))]
    assert [_issue_size(shell.query(i)) for i in range(4)] == [100, 50, 25, 10]
    assert issues_mod.ISSUE_PAGE_SIZES == (100, 50, 25, 10)
    page = _page("issues", [_node(1)])
    shell = _Flaky([_gateway(), page])
    assert [i.number for i in Issues("o", "r", shell=shell).updated_since(MARK)] == [1]
    assert _issue_size(shell.query(1)) == 50
    assert "filterBy" in shell.query(1)


def test_issue_parses_comments_and_total_and_queries_ask_for_them() -> None:
    node = {
        **_node(3),
        "comments": {
            "totalCount": 120,
            "nodes": [
                {
                    "author": {"__typename": "Bot", "login": "stale"},
                    "body": "b",
                    "createdAt": "2026-01-02T00:00:00Z",
                    "isMinimized": True,
                },
                None,
                {"author": None, "body": "h", "createdAt": "2026-01-03T00:00:00Z"},
            ],
        },
    }
    issue = Issue.from_graphql_node(node)
    assert issue.comments_total == 120
    assert [(c.author, c.bot, c.minimized) for c in issue.comments] == [
        ("stale", True, True),
        (None, False, False),
    ]
    assert Issue.from_graphql_node(_node(4)).comments == ()
    for q in (issues_mod._list_query(100), issues_mod._since_query(100)):
        assert issues_mod._ISSUE_FIELDS in q
        assert "comments(first: 100)" in q and "totalCount" in q


def _full_pr_node() -> dict:
    def rev(login, typename="User", body="", state="COMMENTED", **kw):
        return {
            "author": {"__typename": typename, "login": login},
            "state": state,
            "body": body,
            "submittedAt": "2026-01-05T00:00:00Z",
            **kw,
        }

    return {
        **_node(9),
        "author": {"login": "alice"},
        "comments": {
            "totalCount": 3,
            "nodes": [
                {
                    "author": {"__typename": "Bot", "login": "codecov"},
                    "body": "cov",
                    "createdAt": "2026-01-02T00:00:00Z",
                },
                {
                    "author": {"__typename": "User", "login": "dependabot[bot]"},
                    "body": "x",
                    "createdAt": "2026-01-02T00:00:00Z",
                },
                {
                    "author": None,
                    "body": "hidden",
                    "createdAt": "2026-01-02T00:00:00Z",
                    "isMinimized": True,
                },
            ],
        },
        "reviews": {
            "nodes": [
                {
                    "author": {"login": "bob"},
                    "submittedAt": "2026-01-04T00:00:00Z",
                }
            ]
        },
        "reviewSummaries": {
            "nodes": [
                rev("bob", body="Looks good, but why not X?", state="APPROVED"),
                rev("bob", body="   "),
                rev("copilot", typename="Bot", body="AI summary", isMinimized=True),
                None,
            ]
        },
        "reviewThreads": {"totalCount": 7},
    }


def test_pull_request_parses_bot_minimized_summaries_and_totals() -> None:
    pr = PullRequest.from_graphql_node(_full_pr_node())
    assert [(c.bot, c.minimized) for c in pr.comments] == [
        (True, False),
        (True, False),
        (False, True),
    ]
    assert pr.comments[2].author is None
    assert pr.comments_total == 3
    assert [(r.author, r.bot, r.minimized) for r in pr.review_summaries] == [
        ("bob", False, False),
        ("copilot", True, True),
    ]
    assert pr.review_summaries[0].body == "Looks good, but why not X?"
    assert pr.review_summaries[0].state == "APPROVED"
    assert pr.review_threads_total == 7
    assert pr.first_review_at == "2026-01-04T00:00:00Z"
    bare = PullRequest.from_graphql_node(_node(1))
    assert (bare.review_summaries, bare.review_threads_total, bare.comments_total) == (
        (),
        0,
        0,
    )


def test_pr_fields_ask_for_the_new_selections_and_keep_reviews_first_5() -> None:
    f = prs_mod._PR_FIELDS
    assert (
        "reviews(first: 5, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) {\n"
        "          nodes { author { login } submittedAt }\n        }"
    ) in f
    assert "reviewSummaries: reviews(last: 30" in f
    assert "reviewThreads { totalCount }" in f
    assert "nodes { author { __typename login } body createdAt isMinimized }" in f
    assert prs_mod.PR_PAGE_SIZES == (50, 25, 10, 5)
    assert prs_mod.MIN_DETAILS_BATCH == 2


def test_review_thread_parses_anchor_and_drops_file_level() -> None:
    hunk = "@@ -1,2 +1,3 @@\n ctx\n+    value = compute_the_thing(argument)\n"
    node = {
        "path": "a.py",
        "originalLine": 3,
        "isResolved": True,
        "isOutdated": False,
        "subjectType": "LINE",
        "diffSide": "RIGHT",
        "comments": {
            "totalCount": 12,
            "nodes": [
                {
                    "author": {"__typename": "Bot", "login": "x"},
                    "body": "q",
                    "createdAt": "t",
                    "diffHunk": hunk,
                },
                {"author": {"login": "h"}, "body": "a", "createdAt": "t"},
            ],
        },
    }
    t = ReviewThread.from_graphql_node(node)
    assert (t.path, t.original_line, t.resolved, t.outdated) == ("a.py", 3, True, False)
    assert t.anchor is not None and t.anchor.added is True
    assert t.anchor.lines[-1] == "value = compute_the_thing(argument)"
    assert t.comments_total == 12 and t.comments[0].bot is True
    file_level = {**node, "subjectType": "FILE", "originalLine": None}
    assert ReviewThread.from_graphql_node(file_level).anchor is None
    left = {**node, "diffSide": "LEFT"}
    assert ReviewThread.from_graphql_node(left).anchor is None


# --- Thread pass (plan 62 step 3) ----------------------------------------

_HUNK = "@@ -1,2 +1,3 @@\n ctx\n+    value = compute_the_thing(argument)\n"


def _thread(path: str = "a.py", **extra) -> dict:
    return {
        "path": path,
        "originalLine": 3,
        "isResolved": False,
        "isOutdated": False,
        "subjectType": "LINE",
        "diffSide": "RIGHT",
        "comments": {
            "totalCount": 1,
            "nodes": [
                {
                    "author": {"__typename": "User", "login": "h"},
                    "body": "why?",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "isMinimized": False,
                    "diffHunk": _HUNK,
                }
            ],
        },
        **extra,
    }


def _threads_page(numbers, *, remaining: int | None = 5000, none=()) -> dict:
    repo = {
        f"pr{n}": None
        if n in none
        else {"number": n, "reviewThreads": {"totalCount": 1, "nodes": [_thread()]}}
        for n in numbers
    }
    data: dict = {"repository": repo}
    if remaining is not None:
        data["rateLimit"] = {
            "cost": 1,
            "remaining": remaining,
            "resetAt": "2026-11-26T13:00:00Z",
        }
    return data


def _aliases(query: str) -> list[int]:
    return [int(n) for n in re.findall(r"pr(\d+): pullRequest\(number: \1\)", query)]


def test_thread_pass_batches_of_five_and_asks_rate_limit() -> None:
    shell = CmdShell([])
    assert (
        list(PullRequests("o", "r", shell=shell).review_threads([], point_floor=0))
        == []
    )
    nums = list(range(1, 13))
    shell = CmdShell(
        [_threads_page(nums[:5]), _threads_page(nums[5:10]), _threads_page(nums[10:])]
    )
    run = PullRequests("o", "r", shell=shell).review_threads(nums, point_floor=1000)
    assert shell.cmds == []  # nothing sent until iterated
    got = list(run)
    assert prs_mod.THREAD_BATCH == 5
    assert [_aliases(shell.query(i)) for i in range(3)] == [
        nums[:5],
        nums[5:10],
        nums[10:],
    ]
    q = shell.query(0)
    assert q.startswith("query($owner: String!, $name: String!) {")
    assert "rateLimit { cost remaining resetAt }" in q
    assert "reviewThreads(first: 20)" in q and "$cursor" not in q
    assert (
        "comments(first: 10) { totalCount nodes { author { __typename login } "
        "body createdAt isMinimized diffHunk } }" in q
    )
    assert [n for n, _ in got] == nums
    assert all(len(threads) == 1 for _, threads in got)
    assert (run.stopped, run.pending, run.skipped) == (False, (), ())
    assert (run.remaining, run.reset_at) == (5000, "2026-11-26T13:00:00Z")


def test_thread_pass_halves_to_one_and_skips_a_pr_that_times_out_alone(
    caplog, monkeypatch
) -> None:
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    nums = [1, 2, 3, 4, 5]
    shell = _Flaky(
        [
            _gateway(502),  # 5
            _gateway(504),  # 2
            _truncated(),  # 1: PR 1, skipped
            _threads_page([2]),
            _threads_page([3]),
            _threads_page([4]),
            _threads_page([5]),
        ]
    )
    run = PullRequests("o", "r", shell=shell).review_threads(nums, point_floor=0)
    with caplog.at_level("WARNING"):
        got = [n for n, _ in run]
    assert got == [2, 3, 4, 5]
    assert [_aliases(shell.query(i)) for i in range(4)] == [nums, [1, 2], [1], [2]]
    assert run.skipped == (1,) and run.stopped is False
    assert any(
        "#1" in r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    )


def test_thread_pass_splits_a_batch_on_a_gateway_error() -> None:
    nums = [1, 2, 3, 4, 5]
    shell = _Flaky(
        [_gateway(), _threads_page([1, 2]), _threads_page([3, 4]), _threads_page([5])]
    )
    run = PullRequests("o", "r", shell=shell).review_threads(nums, point_floor=0)
    assert [n for n, _ in run] == nums
    assert [_aliases(shell.query(i)) for i in range(4)] == [nums, [1, 2], [3, 4], [5]]
    assert run.skipped == ()


def test_thread_pass_stops_below_the_floor_before_the_next_request() -> None:
    nums = list(range(1, 18))
    shell = CmdShell(
        [
            _threads_page(nums[:5], remaining=1000),  # at the floor: go on
            _threads_page(nums[5:10], remaining=999),  # below: stop
            _threads_page(nums[10:15]),  # never asked
        ]
    )
    run = PullRequests("o", "r", shell=shell).review_threads(nums, point_floor=1000)
    got = [n for n, _ in run]  # no error raised
    assert got == nums[:10]
    assert len(shell.cmds) == 2
    assert run.stopped is True
    assert run.pending == tuple(nums[10:])
    assert run.remaining == 999


def test_thread_pass_first_request_needs_no_rate_limit() -> None:
    # The first request has no previous response, and a response without
    # rateLimit keeps the pass going.
    shell = CmdShell(
        [_threads_page([1, 2, 3, 4, 5], remaining=None), _threads_page([6])]
    )
    run = PullRequests("o", "r", shell=shell).review_threads(
        list(range(1, 7)), point_floor=10**9
    )
    assert [n for n, _ in run] == [1, 2, 3, 4, 5, 6]
    assert run.stopped is False and run.remaining == 5000


def _not_found_error(alias: str, data: dict, extra: dict | None = None) -> ShellError:
    errors = [
        {
            "type": "NOT_FOUND",
            "path": ["repository", alias],
            "message": "Could not resolve to a PullRequest with the number of 2.",
        }
    ]
    errors += [extra] if extra else []
    out = json.dumps({"data": data, "errors": errors})
    return ShellError(["gh"], 1, out, "gh: Could not resolve to a PullRequest")


def test_thread_pass_tolerates_a_deleted_pr_as_none() -> None:
    page = _threads_page([1, 2, 3], none=(2,), remaining=4000)
    shell = _Flaky([_not_found_error("pr2", page)])
    run = PullRequests("o", "r", shell=shell).review_threads([1, 2, 3], point_floor=0)
    got = dict(run)
    assert got[2] is None
    assert len(got[1]) == 1 and len(got[3]) == 1
    assert run.remaining == 4000  # a tolerated answer still counts for the floor


def test_thread_pass_does_not_tolerate_not_found_mixed_with_another_error() -> None:
    page = _threads_page([1, 2], none=(2,))
    other = {"type": "INTERNAL", "path": ["repository", "pr1"], "message": "x"}
    shell = _Flaky([_not_found_error("pr2", page, other)])
    with pytest.raises(GitHubError):
        list(PullRequests("o", "r", shell=shell).review_threads([1, 2], point_floor=0))


def test_thread_pass_raises_other_errors_after_the_yielded_batches() -> None:
    nums = list(range(1, 12))
    shell = _Flaky(
        [_threads_page(nums[:5]), ShellError(["gh"], 1, "", "HTTP 403 forbidden")]
    )
    got: list[int] = []
    with pytest.raises(GitHubError, match="403"):
        for n, _ in PullRequests("o", "r", shell=shell).review_threads(
            nums, point_floor=0
        ):
            got.append(n)
    assert got == nums[:5]
    assert len(shell.cmds) == 2


def test_thread_pass_yields_nothing_of_a_malformed_batch() -> None:
    page = _threads_page([1, 2])
    del page["repository"]["pr2"]
    got: list[int] = []
    with pytest.raises(GitHubError, match="pr2"):
        for n, _ in PullRequests("o", "r", shell=CmdShell([page])).review_threads(
            [1, 2], point_floor=0
        ):
            got.append(n)
    assert got == []


def test_thread_pass_parses_anchor_from_the_first_comment_only() -> None:
    other_hunk = "@@ -9 +9 @@\n+    something_else_entirely_different(here)\n"
    line = _thread()
    line["comments"]["nodes"].append(
        {
            "author": {"__typename": "User", "login": "author"},
            "body": "because",
            "createdAt": "2026-01-02T00:00:00Z",
            "isMinimized": False,
            "diffHunk": other_hunk,
        }
    )
    line["comments"]["totalCount"] = 2
    file_level = _thread("b.py", subjectType="FILE", originalLine=None)
    left = _thread("c.py", diffSide="LEFT")
    page = {
        "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "t"},
        "repository": {
            "pr7": {
                "number": 7,
                "reviewThreads": {"totalCount": 3, "nodes": [line, file_level, left]},
            }
        },
    }
    shell = CmdShell([page])
    [(number, threads)] = list(
        PullRequests("o", "r", shell=shell).review_threads([7], point_floor=0)
    )
    assert number == 7
    t, f, lt = threads
    assert t.anchor is not None and t.anchor.added is True
    assert t.anchor.lines[-1] == "value = compute_the_thing(argument)"
    assert [c.author for c in t.comments] == ["h", "author"]
    assert t.comments_total == 2
    assert (f.path, f.original_line, f.anchor) == ("b.py", None, None)
    assert (lt.path, lt.anchor) == ("c.py", None)
