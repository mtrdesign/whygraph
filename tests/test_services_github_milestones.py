"""Milestone listing in the GitHub service (plan 38, step 21)."""

from __future__ import annotations

from test_services_github_pagination import CmdShell, _page

from whygraph.services.github import milestones as ms_mod
from whygraph.services.github.client import GitHubClient
from whygraph.services.github.milestones import Milestone, Milestones


def _node(number: int, **kw) -> dict:
    node = {
        "number": number,
        "title": f"M{number}",
        "description": "",
        "state": "OPEN",
        "dueOn": "2026-12-01T00:00:00Z",
        "closedAt": None,
        "updatedAt": "2026-10-01T00:00:00Z",
        "progressPercentage": 42.5,
        "url": f"https://example.test/milestone/{number}",
    }
    node.update(kw)
    return node


def test_from_graphql_node_parses_fields() -> None:
    m = Milestone.from_graphql_node(_node(3, state="CLOSED", description="d"))
    assert (m.number, m.state, m.description) == (3, "closed", "d")
    assert m.due_on == "2026-12-01T00:00:00Z" and m.progress_pct == 42.5
    assert m.html_url.endswith("/milestone/3")


def test_empty_description_and_missing_progress_become_none() -> None:
    m = Milestone.from_graphql_node(_node(1, progressPercentage=None))
    assert m.description is None and m.progress_pct is None


def test_query_lists_open_and_closed_with_progress() -> None:
    q = ms_mod._LIST_QUERY
    assert "states: [OPEN, CLOSED]" in q
    assert "progressPercentage" in q and "$cursor" in q


def test_iteration_paginates() -> None:
    shell = CmdShell(
        [
            _page("milestones", [_node(1), _node(2)], more=True),
            _page("milestones", [_node(3)]),
        ]
    )
    got = list(Milestones("o", "r", shell=shell))
    assert [m.number for m in got] == [1, 2, 3]
    assert len(shell.cmds) == 2
    assert shell.args(0)["owner"] == "o"
    assert shell.args(1)["cursor"] == "c1"


def test_client_exposes_milestones() -> None:
    client = GitHubClient("o", "r")
    assert isinstance(client.milestones, Milestones)
