"""Incremental iteration in the GitHub service (incremental-scan plan, step 1)."""

from __future__ import annotations

import json
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
from whygraph.services.github.issues import Issues
from whygraph.services.github.pull_request import PullRequest
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
    for q in (prs_mod._list_query(100), issues_mod._LIST_QUERY):
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


def test_pull_request_list_fails_after_three_gateway_errors() -> None:
    shell = _Flaky([_gateway(), _gateway(), _gateway()])
    with pytest.raises(GitHubError, match="HTTP 502"):
        [*iter(PullRequests("o", "r", shell=shell))]
    assert _size(shell.query(2)) == 10


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


def test_forbidden_review_requests_are_dropped_not_fatal(caplog) -> None:
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
