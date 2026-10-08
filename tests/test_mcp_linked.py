"""The MCP tools of a project linked to a platform (M2e step 8, plan section 4.10).

A linked project is a local checkout with **no** WhyGraph database: the
tool bodies blame the working tree here and ask the platform about the
**pushed** SHAs only. These tests drive the real tool functions under a
:class:`~whygraph.core.context.ProjectContext` whose ``remote`` is a
:class:`~whygraph.portal.linked.LinkedProject` over
``tests/platform_fake.py``, plugged in as an ``httpx.MockTransport`` - so
nothing touches the network and every request body is recorded.

What they pin:

* every line's ``push_status`` - uncommitted, not pushed, pushed but
  unscanned, pushed off the default branch;
* that **only** pushed hunks leave the machine, with the request bodies
  asserted field by field, and that a symbol name whose declaration line
  is not pushed is withheld (plan section 0.2 #8);
* the merged list's order, which must follow parsed instants rather than
  the two sides' differently-shaped timestamp strings;
* offline, revoked and removed behaviour, and the local-git fallback for
  a commit the platform does not know;
* that no code path opens a local WhyGraph database, backfills a
  description or reads the rationale cache.

The repository every test shares looks like this (``a.py``, six lines)::

    1  one            c1   pushed, on origin/main
    2  two            c1
    3  inserted       c3   committed locally, never pushed
    4  THREE-changed  c2   pushed, on origin/main
    5  four           c1
    6  uncommitted    --   not committed at all

so one blame of lines 1-6 produces all four push states at once, and the
symbol ``pkg.mixed`` (lines 3-5) is declared on an unpushed line while its
body is pushed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import build_fake_codegraph_db
from platform_fake import ORG, PLATFORM_ORIGIN, SLUG, TOKEN, FakePlatform, error_body
from whygraph.api_v1 import PLATFORM_BUDGET_MESSAGE
from whygraph.core.config import Config
from whygraph.core.context import ProjectContext, ProjectContextError, use_project
from whygraph.db import get_engine, get_session
from whygraph.mcp.area_history import whygraph_area_history
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import (
    PUSH_NOT_ON_DEFAULT,
    PUSH_NOT_PUSHED,
    PUSH_PENDING_SCAN,
    PUSH_UNCOMMITTED,
    whygraph_evidence_for,
)
from whygraph.mcp.rationale import whygraph_rationale_brief
from whygraph.mcp.resources import (
    _commit_resource,
    _issue_resource,
    _pr_resource,
    _repo_overview_resource,
)
from whygraph.portal.linked import LinkedProject
from whygraph.portal.platform_client import PlatformHttp, api_origin_for

API_ORIGIN = api_origin_for(PLATFORM_ORIGIN, ORG)
"""Where the fake platform's org host lives (computed, never taken from it)."""

C1_DATE = "2025-12-01T12:00:00+00:00"
C2_DATE = "2026-01-01T00:30:00+00:00"
C3_DATE = "2025-11-01T12:00:00+00:00"

# 2026-01-01T01:00:00+03:00 is 2025-12-31T22:00:00Z - *earlier* than C2,
# although the strings sort the other way round.
PLATFORM_C1_DATE = "2026-01-01T01:00:00+03:00"

NODES = [
    {
        "id": "n_head",
        "kind": "function",
        "name": "head",
        "qualified_name": "pkg.head",
        "file_path": "a.py",
        "language": "python",
        "start_line": 1,
        "end_line": 2,
        "docstring": None,
        "signature": "def head()",
    },
    {
        "id": "n_mixed",
        "kind": "function",
        "name": "mixed",
        "qualified_name": "pkg.mixed",
        "file_path": "a.py",
        "language": "python",
        "start_line": 3,
        "end_line": 5,
        "docstring": None,
        "signature": "def mixed()",
    },
]


# ---------------------------------------------------------------------------
# The checkout
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, date: str | None = None) -> str:
    """Run ``git`` in ``root`` with a fixed identity (and optionally a date)."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    if date is not None:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, env=env
    ).stdout


def _write(root: Path, *lines: str) -> None:
    (root / "a.py").write_text("".join(f"{line}\n" for line in lines))


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    """The checkout of the module docstring, plus its CodeGraph index."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "remote", "add", "origin", "https://github.com/acme/demo.git")

    _write(root, "one", "two", "THREE", "four")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "first", date=C1_DATE)
    c1 = _head(root)

    _write(root, "one", "two", "THREE-changed", "four")
    _git(root, "commit", "-q", "-a", "-m", "second", date=C2_DATE)
    c2 = _head(root)

    # What the platform has fetched: origin/main is at the second commit.
    _git(root, "update-ref", "refs/remotes/origin/main", c2)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    _write(root, "one", "two", "inserted", "THREE-changed", "four")
    _git(root, "commit", "-q", "-a", "-m", "third", date=C3_DATE)
    c3 = _head(root)

    _write(root, "one", "two", "inserted", "THREE-changed", "four", "uncommitted")

    (root / ".codegraph").mkdir()
    build_fake_codegraph_db(root / ".codegraph" / "codegraph.db", nodes=NODES, edges=[])
    return SimpleNamespace(root=root, c1=c1, c2=c2, c3=c3)


@pytest.fixture
def fake() -> FakePlatform:
    """A platform holding the ``demo`` project and this machine's token."""
    platform = FakePlatform()
    platform.add_token()
    return platform


@pytest.fixture
def remote(fake: FakePlatform) -> LinkedProject:
    """The project's platform, over the fake's transport."""
    return LinkedProject(
        PlatformHttp(
            platform_origin=PLATFORM_ORIGIN,
            api_origin=API_ORIGIN,
            token=TOKEN,
            transport=httpx.MockTransport(fake.handle),
        ),
        slug=SLUG,
        org=ORG,
        platform_origin=PLATFORM_ORIGIN,
    )


@pytest.fixture
def linked(repo: SimpleNamespace, remote: LinkedProject) -> ProjectContext:
    """The bound context of a linked project over :func:`repo`."""
    return ProjectContext(
        slug="demo",
        root=repo.root,
        config=Config(
            whygraph_db=repo.root / ".whygraph" / "whygraph.db",
            codegraph_db=repo.root / ".codegraph" / "codegraph.db",
        ),
        remote=remote,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def platform_item(sha: str, *, committed_at: str, source: str = "blame") -> dict:
    """One evidence item as the platform's database would answer it."""
    return {
        "commit": {
            "sha": sha,
            "llm_description": "what the diff did",
            "subject": "on the platform",
            "body": "",
            "author_name": "Platform",
            "author_email": "platform@example.com",
            "committed_at": committed_at,
        },
        "pull_requests": [],
        "issues": [],
        "source": source,
    }


def answer_evidence(fake: FakePlatform, items: list[dict], unknown: list[str]) -> None:
    """Make ``POST /evidence`` answer with ``items`` and ``unknown``."""
    fake.responses["evidence"] = (
        200,
        {
            "evidence": items,
            "unknown_shas": unknown,
            "project": fake.projects[SLUG],
        },
    )


def bodies(fake: FakePlatform, tail: str) -> list[dict]:
    """The parsed request bodies the fake saw for ``/api/v1/.../<tail>``."""
    return [
        json.loads(request.content)
        for request in fake.requests
        if request.url.path.endswith(f"/{tail}")
    ]


def statuses(payload: dict) -> dict[str, str | None]:
    """``sha -> push_status`` of a tool answer's evidence list."""
    return {
        item["commit"]["sha"]: item.get("push_status") for item in payload["evidence"]
    }


def sources(payload: dict) -> dict[str, str]:
    """``sha -> source`` of a tool answer's evidence list."""
    return {item["commit"]["sha"]: item["source"] for item in payload["evidence"]}


UNCOMMITTED_SHA = "0" * 40


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_mcp_linked_labels_uncommitted_and_unpushed(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """Every line the platform cannot account for says why, in one answer."""
    answer_evidence(
        fake,
        [platform_item(repo.c1, committed_at=C1_DATE)],
        [repo.c2],  # pushed and on the default branch, but not scanned yet
    )
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)

    assert statuses(payload) == {
        repo.c1: None,  # answered by the platform: no push_status at all
        repo.c2: PUSH_PENDING_SCAN,
        repo.c3: PUSH_NOT_PUSHED,
        UNCOMMITTED_SHA: PUSH_UNCOMMITTED,
    }
    assert sources(payload) == {
        repo.c1: "blame",
        repo.c2: "local",
        repo.c3: "local",
        UNCOMMITTED_SHA: "local",
    }
    # A locally built entry carries blame's metadata and nothing invented.
    local = next(i for i in payload["evidence"] if i["commit"]["sha"] == repo.c3)
    assert local["commit"]["llm_description"] is None
    assert local["commit"]["author_name"] == "Dev"
    assert local["commit"]["subject"] == "third"
    assert (local["pull_requests"], local["issues"]) == ([], [])
    # The platform's own entry is passed through, description and all.
    remote_item = next(i for i in payload["evidence"] if i["commit"]["sha"] == repo.c1)
    assert remote_item["commit"]["llm_description"] == "what the diff did"
    assert payload["platform"] == {"status": "ok", "url": API_ORIGIN}
    assert payload["target"] == {
        "path": "a.py",
        "line_start": 1,
        "line_end": 6,
        "qualified_name": None,
    }


def test_mcp_linked_labels_a_commit_pushed_off_the_default_branch(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """A pushed SHA the platform does not scan is told apart from an unscanned one."""
    _git(repo.root, "update-ref", "refs/remotes/origin/feature", repo.c3)
    answer_evidence(fake, [], [repo.c1, repo.c2, repo.c3])
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)

    assert statuses(payload) == {
        repo.c1: PUSH_PENDING_SCAN,
        repo.c2: PUSH_PENDING_SCAN,
        repo.c3: PUSH_NOT_ON_DEFAULT,
        UNCOMMITTED_SHA: PUSH_UNCOMMITTED,
    }
    # Now that a remote ref holds it, the third commit *is* asked about.
    assert {h["sha"] for h in bodies(fake, "evidence")[0]["hunks"]} == {
        repo.c1,
        repo.c2,
        repo.c3,
    }


# ---------------------------------------------------------------------------
# What leaves the machine
# ---------------------------------------------------------------------------


def test_mcp_linked_sends_only_pushed_hunks(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """Mutation 7: an uncommitted or unpushed hunk must never be sent."""
    answer_evidence(fake, [], [])
    with use_project(linked):
        whygraph_evidence_for(path="a.py", line_start=1, line_end=6, limit=7)

    sent = bodies(fake, "evidence")
    assert len(sent) == 1
    body = sent[0]
    # Blame order, the unpushed and uncommitted hunks dropped.
    assert [hunk["sha"] for hunk in body["hunks"]] == [repo.c1, repo.c2]
    assert body["limit"] == 7
    assert body["target"] == {
        "path": "a.py",
        "line_start": 1,
        "line_end": 6,
        "qualified_name": None,
    }
    # Every origin names a path and a range at a pushed commit.
    for hunk in body["hunks"]:
        assert hunk["origins"]
        for origin in hunk["origins"]:
            assert origin["path"] == "a.py"
            assert 1 <= origin["start"] <= origin["end"]
    # Nothing unpushed appears anywhere in the serialized request.
    raw = json.dumps(body)
    assert repo.c3 not in raw
    assert UNCOMMITTED_SHA not in raw
    assert "uncommitted" not in raw
    assert "inserted" not in raw


def test_mcp_linked_makes_no_request_without_a_pushed_hunk(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """Lines that are all local are answered locally, with no round trip."""
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=3, line_end=3)
    assert fake.requests == []
    assert statuses(payload) == {repo.c3: PUSH_NOT_PUSHED}
    assert payload["platform"] == {"status": "ok", "url": API_ORIGIN}

    with use_project(linked), pytest.raises(WhyGraphError) as caught:
        whygraph_rationale_brief(path="a.py", line_start=3, line_end=3)
    assert "no pushed history" in str(caught.value)
    assert fake.requests == []


def test_mcp_linked_hides_uncommitted_symbol_name(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """Mutation 7: a symbol name goes only when its declaration line is pushed."""
    answer_evidence(fake, [], [])
    with use_project(linked):
        # pkg.mixed (lines 3-5) is declared on the unpushed third commit's
        # line, so the name stays here - but its pushed body lines still go.
        payload = whygraph_evidence_for(qualified_name="pkg.mixed")
    body = bodies(fake, "evidence")[0]
    assert body["target"] == {
        "path": "a.py",
        "line_start": 3,
        "line_end": 5,
        "qualified_name": None,
    }
    assert "pkg.mixed" not in json.dumps(body)
    assert [hunk["sha"] for hunk in body["hunks"]] == [repo.c2, repo.c1]
    # The tool's own answer still names the symbol for the agent.
    assert payload["target"]["qualified_name"] == "pkg.mixed"

    fake.requests.clear()
    with use_project(linked):
        whygraph_evidence_for(qualified_name="pkg.head")
    head_body = bodies(fake, "evidence")[0]
    assert head_body["target"]["qualified_name"] == "pkg.head"
    assert head_body["target"]["line_start"] == 1


def test_mcp_linked_rationale_sends_the_same_reduced_target(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """The card is the platform's; the request obeys the same privacy rules."""
    with use_project(linked):
        card = whygraph_rationale_brief(qualified_name="pkg.mixed")
    body = bodies(fake, "rationale")[0]
    assert body["target"]["qualified_name"] is None
    assert [hunk["sha"] for hunk in body["hunks"]] == [repo.c2, repo.c1]
    assert "limit" not in body
    assert card["purpose"] == "p"
    assert card["platform"] == {"status": "ok", "url": API_ORIGIN}


def test_mcp_linked_rationale_shows_the_platforms_budget_refusal(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """A platform's ``403 budget_exceeded`` (M2f-2): its scope-neutral message."""
    fake.responses["rationale"] = (
        403,
        error_body("budget_exceeded", PLATFORM_BUDGET_MESSAGE, scope="org"),
    )
    with use_project(linked), pytest.raises(WhyGraphError) as caught:
        whygraph_rationale_brief(qualified_name="pkg.mixed")
    assert PLATFORM_BUDGET_MESSAGE in str(caught.value)
    # An answer about one request, not about the link: the link stays ok.
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)
    assert payload["platform"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_mcp_linked_sorts_by_parsed_time(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """The two sides time-stamp differently, so the merge sorts on instants.

    The platform's row for the first commit carries ``git``'s ``%cI`` with a
    ``+03:00`` offset; a blame hunk carries UTC. Sorted as strings the
    platform row would come first; sorted as instants the second commit does.
    """
    answer_evidence(
        fake,
        [platform_item(repo.c1, committed_at=PLATFORM_C1_DATE)],
        [repo.c2, repo.c3],
    )
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)

    order = [item["commit"]["sha"] for item in payload["evidence"]]
    assert order == [UNCOMMITTED_SHA, repo.c2, repo.c1, repo.c3]
    # The naive string sort would have swapped the middle pair.
    stamps = [item["commit"]["committed_at"] for item in payload["evidence"]]
    assert stamps[1] < stamps[2]


def test_mcp_linked_caps_the_merged_list(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    answer_evidence(fake, [platform_item(repo.c1, committed_at=C1_DATE)], [repo.c2])
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6, limit=2)
    assert len(payload["evidence"]) == 2
    assert bodies(fake, "evidence")[0]["limit"] == 2


# ---------------------------------------------------------------------------
# Offline, revoked, removed
# ---------------------------------------------------------------------------


def test_mcp_linked_offline_fallback(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """Unreachable: evidence still answers, from blame alone; the rest say so."""
    fake.failure = "timeout"
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)
    assert payload["platform"] == {"status": "unreachable", "url": API_ORIGIN}
    assert statuses(payload) == {
        repo.c1: PUSH_PENDING_SCAN,
        repo.c2: PUSH_PENDING_SCAN,
        repo.c3: PUSH_NOT_PUSHED,
        UNCOMMITTED_SHA: PUSH_UNCOMMITTED,
    }
    assert set(sources(payload).values()) == {"local"}

    for call in (
        lambda: whygraph_rationale_brief(path="a.py", line_start=1, line_end=6),
        lambda: whygraph_area_history("a.py"),
        lambda: _repo_overview_resource(),
        lambda: _pr_resource(7),
    ):
        with use_project(linked), pytest.raises(WhyGraphError) as caught:
            call()
        assert "platform unreachable" in str(caught.value)
        assert API_ORIGIN in str(caught.value)


def test_mcp_linked_reports_revoked(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """A revoked token names the reason and the two ways out."""
    fake.revoke_token(TOKEN, "admin_revoked")
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)
    assert payload["platform"] == {"status": "revoked", "url": API_ORIGIN}
    assert set(sources(payload).values()) == {"local"}

    with use_project(linked), pytest.raises(WhyGraphError) as caught:
        whygraph_area_history("a.py")
    message = str(caught.value)
    assert "revoked" in message and "admin_revoked" in message
    assert "reconnect" in message and "remove it from this machine" in message
    assert f"{ORG}/{SLUG}" in message and PLATFORM_ORIGIN in message
    assert TOKEN not in message


def test_mcp_linked_reports_removed(linked: ProjectContext, fake: FakePlatform) -> None:
    """A project deleted on the platform is ``removed``, not merely revoked."""
    fake.revoke_token(TOKEN, "project_deleted")
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)
    assert payload["platform"]["status"] == "removed"

    with use_project(linked), pytest.raises(WhyGraphError) as caught:
        whygraph_area_history("a.py")
    assert "was removed on the platform" in str(caught.value)


def test_mcp_linked_reports_a_malformed_reply_as_unreachable(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    """A reply that is not the expected shape is no answer about the link."""
    fake.responses["evidence"] = (200, {"evidence": "not a list"})
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)
    assert payload["platform"]["status"] == "unreachable"


# ---------------------------------------------------------------------------
# History and resources
# ---------------------------------------------------------------------------


def test_mcp_linked_history_comes_from_the_platform(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    fake.responses["history"] = (
        200,
        {
            "path": "a.py",
            "include_renames": True,
            "evidence": [platform_item(repo.c1, committed_at=C1_DATE)],
            "project": fake.projects[SLUG],
        },
    )
    with use_project(linked):
        payload = whygraph_area_history("a.py", limit=5, include_renames=False)
    assert payload["path"] == "a.py"
    assert payload["include_renames"] is False
    assert [i["commit"]["sha"] for i in payload["evidence"]] == [repo.c1]
    assert payload["platform"] == {"status": "ok", "url": API_ORIGIN}
    sent = next(r for r in fake.requests if r.url.path.endswith("/history"))
    assert dict(sent.url.params) == {
        "path": "a.py",
        "limit": "5",
        "include_renames": "false",
    }


def test_mcp_linked_history_withholds_an_unpushed_path(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    """A file only this checkout has is never named to the platform."""
    (linked.root / "secret.py").write_text("x = 1\n")
    with use_project(linked):
        payload = whygraph_area_history("secret.py")
    assert payload["evidence"] == []
    assert fake.requests == []


def test_mcp_linked_commit_resource_falls_back_to_local_git(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """A commit the platform does not hold is served from the checkout."""
    fake.responses["commit"] = (
        404,
        {"error": "not found in this project's history", "code": "not_found"},
    )
    with use_project(linked):
        payload = _commit_resource(repo.c3)
    assert payload["source"] == "local"
    assert payload["push_status"] == PUSH_NOT_PUSHED
    assert payload["commit"]["subject"] == "third"
    assert payload["commit"]["llm_description"] is None
    assert payload["linked_prs"] == []
    assert payload["platform"] == {"status": "ok", "url": API_ORIGIN}

    with use_project(linked):
        missing = _commit_resource("b" * 40)
    assert missing["error"] == "not_found"
    assert missing["platform"]["status"] == "ok"


def test_mcp_linked_commit_resource_prefers_the_platform(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    fake.responses["commit"] = (
        200,
        {
            "commit": {"sha": repo.c1, "subject": "on the platform"},
            "linked_prs": [{"number": 7}],
            "project": fake.projects[SLUG],
        },
    )
    with use_project(linked):
        payload = _commit_resource(repo.c1)
    assert payload["commit"]["subject"] == "on the platform"
    assert payload["linked_prs"] == [{"number": 7}]
    assert "project" not in payload  # the platform's bookkeeping is stripped


def test_mcp_linked_pr_and_issue_not_found_is_content(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    """Rule 2 of the resources module survives the platform hop."""
    for route, read, key in (
        ("pr", lambda: _pr_resource(99), "number"),
        ("issue", lambda: _issue_resource(99), "number"),
    ):
        fake.responses[route] = (404, {"error": "not found", "code": "not_found"})
        with use_project(linked):
            payload = read()
        assert payload["error"] == "not_found"
        assert payload[key] == 99
        assert payload["platform"] == {"status": "ok", "url": API_ORIGIN}


def test_mcp_linked_overview_comes_from_the_platform(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    fake.responses["overview"] = (
        200,
        {"counts": {"commits": 3}, "project": fake.projects[SLUG]},
    )
    with use_project(linked):
        payload = _repo_overview_resource()
    assert payload["counts"] == {"commits": 3}
    assert payload["platform"] == {"status": "ok", "url": API_ORIGIN}


# ---------------------------------------------------------------------------
# No local database, no LLM spend
# ---------------------------------------------------------------------------


def test_mcp_linked_never_creates_whygraph_db(
    linked: ProjectContext,
    repo: SimpleNamespace,
    fake: FakePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation 6: the engine refuses a linked project before it can create a file.

    The HTTP half of this is
    ``tests/test_portal_linked_scans.py::test_linked_project_never_creates_whygraph_db``;
    this is the MCP half - the tool bodies and the engine guard they rely on.
    """
    db = repo.root / ".whygraph" / "whygraph.db"

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a linked project must not reach this")

    monkeypatch.setattr(
        "whygraph.mcp.evidence.backfill_evidence_descriptions", forbidden
    )
    monkeypatch.setattr("whygraph.mcp.rationale.lookup_cached", forbidden)
    monkeypatch.setattr("whygraph.mcp.rationale.store_cached", forbidden)
    monkeypatch.setattr("whygraph.mcp.rationale.collect_evidence", forbidden)
    monkeypatch.setattr("whygraph.mcp.path_history.area_history_commits", forbidden)

    answer_evidence(fake, [platform_item(repo.c1, committed_at=C1_DATE)], [repo.c2])
    with use_project(linked):
        with pytest.raises(ProjectContextError) as caught:
            with get_session():
                pass
        assert "linked to a WhyGraph platform" in str(caught.value)
        with pytest.raises(ProjectContextError):
            get_engine()

        assert whygraph_evidence_for(path="a.py", line_start=1, line_end=6)["evidence"]
        assert whygraph_rationale_brief(path="a.py", line_start=1, line_end=6)
        assert whygraph_area_history("a.py")["evidence"] == []
        assert _commit_resource(repo.c1)
        assert _repo_overview_resource()

    assert not db.exists()
    assert not db.parent.exists()


def test_engine_opens_normally_without_a_remote(repo: SimpleNamespace) -> None:
    """The control: the same context without a ``remote`` still opens its DB."""
    plain = ProjectContext(
        slug="demo",
        root=repo.root,
        config=Config(whygraph_db=repo.root / ".whygraph" / "whygraph.db"),
    )
    with use_project(plain):
        assert get_engine() is not None
    assert (repo.root / ".whygraph").is_dir()
