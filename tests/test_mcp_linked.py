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
from platform_fake import (
    ORG,
    PLATFORM_ORIGIN,
    RATIONALE_ITEMS,
    SLUG,
    TOKEN,
    FakePlatform,
    error_body,
)
from whygraph.api_v1 import PLATFORM_BUDGET_MESSAGE
from whygraph.core.config import Config
from whygraph.core.context import (
    ProjectContext,
    ProjectContextError,
    use_actor,
    use_project,
)
from whygraph.db import get_engine, get_session
from whygraph.mcp import targets as targets_module
from whygraph.mcp.area_history import whygraph_area_history
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import (
    PUSH_NOT_ON_DEFAULT,
    PUSH_NOT_PUSHED,
    PUSH_PENDING_SCAN,
    PUSH_UNCOMMITTED,
    whygraph_evidence_for,
)
from whygraph.mcp.notes_linked import linked_notes, linked_propose
from whygraph.mcp.rationale import whygraph_rationale_brief
from whygraph.mcp.targets import Target
from whygraph.mcp.resources import (
    _commit_resource,
    _issue_resource,
    _pr_resource,
    _repo_overview_resource,
)
from whygraph.portal import db as portal_db
from whygraph.portal.linked import LinkedProject
from whygraph.portal.paths import project_db_path
from whygraph.portal.platform_client import PlatformHttp, api_origin_for
from whygraph.services.git import Repository

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


def test_default_remote_refs_keeps_remote_tracking_only(
    repo: SimpleNamespace,
) -> None:
    """Remote-tracking refs pass unchanged; ``refs/heads/*`` is dropped."""
    from whygraph.mcp.evidence import default_remote_refs
    from whygraph.services.git import Repository

    _git(repo.root, "branch", "-f", "develop", repo.c3)
    assert Repository(repo.root).default_branch_refs == (
        "refs/remotes/origin/main",
        "refs/heads/main",
    )
    assert default_remote_refs(Repository(repo.root)) == ("refs/remotes/origin/main",)

    # A local-only default branch (override, no remote ref) yields ().
    local_only = Repository(repo.root, default_branch="develop")
    assert local_only.default_branch_refs == ("refs/heads/develop",)
    assert default_remote_refs(local_only) == ()


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
            # What the portal forces (its side keeps the key; _refuse_linked
            # guards it). The child's config has none (plan decision D23).
            whygraph_db=project_db_path(portal_db.data_dir(), "local", "demo"),
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
    return {item["sha"]: item.get("push_status") for item in payload["evidence"]}


def sources(payload: dict) -> dict[str, str]:
    """``sha -> source`` of a tool answer's evidence list."""
    return {item["sha"]: item["source"] for item in payload["evidence"]}


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
    local = next(i for i in payload["evidence"] if i["sha"] == repo.c3)
    assert local["llm_description"] is None
    assert local["author"] == "Dev"
    assert local["subject"] == "third"
    assert (local["pull_requests"], local["issues"]) == ([], [])
    # The platform's own entry is passed through, description and all.
    remote_item = next(i for i in payload["evidence"] if i["sha"] == repo.c1)
    assert remote_item["llm_description"] == "what the diff did"
    # Compact items: the platform's nested item is shaped like a local one.
    assert "commit" not in remote_item and "text" not in remote_item
    assert payload["describing"] == 0  # descriptions are the platform's
    assert payload["platform"] == {
        "status": "ok",
        "url": API_ORIGIN,
        "answered": True,
    }
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
    assert payload["platform"] == {
        "status": "ok",
        "url": API_ORIGIN,
        "answered": False,
    }

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
    # The platform's items pass through as they came: no empty `url` or
    # `quote` is added, and nothing is flattened to a string (plan 54 D5).
    assert card["constraints"] == list(RATIONALE_ITEMS)
    assert card["risks"] == [RATIONALE_ITEMS[1]]


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


DEPTH_FIELDS = ("intent", "depth", "cached_only")
"""The ``RationaleIn`` fields a platform with depth levels takes (plan 62 section 14.4)."""


def test_mcp_linked_rationale_sends_depth_only_to_a_platform_that_has_it(
    repo: SimpleNamespace,
) -> None:
    """An older platform's ``meta``: none of the new fields, and its old card validates."""
    fake = FakePlatform(depth_supported=False)
    ctx = linked_over(repo, fake)
    with use_project(ctx):
        card = whygraph_rationale_brief(
            qualified_name="pkg.mixed",
            intent="review",
            depth="medium",
            cached_only=True,
        )
    body = bodies(fake, "rationale")[0]
    assert not set(DEPTH_FIELDS) & set(body)
    assert card["purpose"] == "p"
    for key in ("depth", "coverage", "deeper", "status", "match", "stale"):
        assert key not in card
    assert all("check" not in item for item in card["constraints"])
    # The capability was read once, and serves the plans branch too.
    assert len(meta_requests(fake)) == 1


def test_mcp_linked_rationale_without_depth_inputs_reads_no_meta(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    with use_project(linked):
        whygraph_rationale_brief(qualified_name="pkg.mixed")
    assert not set(DEPTH_FIELDS) & set(bodies(fake, "rationale")[0])
    # `meta` is read by the plans branch only, after the card.
    paths = [r.url.path for r in fake.requests]
    assert paths.index("/api/v1/meta") > paths.index(
        f"/api/v1/projects/{SLUG}/rationale"
    )


def test_mcp_linked_rationale_on_a_platform_with_depth_levels(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    """The set fields go; the card's depth, coverage and deeper come back.

    A ``check`` or ``coverage.verification`` from a platform built before
    R49 is dropped, never relayed.
    """
    checked = {
        **RATIONALE_ITEMS[0],
        "check": {"verdict": "holds", "reason": "line 2 still does it"},
    }
    depth = {
        "requested": "medium",
        "resolved": "medium",
        "reason": "Medium was asked for.",
    }
    coverage = {
        "full_prs_demoted": 0,
        "threads_lost": 0,
        "trimmed": [],
        "descriptions_missing": 0,
        "line_commits": 2,
        "context_commits": 0,
        "context_omitted": 0,
        "verification": "verified",
        "source": "included",
    }
    fake.responses["rationale"] = (
        200,
        {
            "target": {"path": "a.py", "line_start": 1, "line_end": 2},
            "purpose": "p",
            "why": "w",
            "constraints": [checked],
            "tradeoffs": [],
            "risks": [],
            "model": "m",
            "provider": "x",
            "cached_at": "2026-10-05T10:00:00Z",
            "evidence_count": {"commits": 2, "prs": 0, "issues": 0},
            "depth": depth,
            "coverage": coverage,
            "deeper": None,
            "project": fake.projects[SLUG],
        },
    )
    with use_project(linked):
        card = whygraph_rationale_brief(
            qualified_name="pkg.mixed", intent="edit", depth="medium"
        )
    body = bodies(fake, "rationale")[0]
    assert (body["intent"], body["depth"]) == ("edit", "medium")
    assert "cached_only" not in body  # None is left out
    assert card["depth"] == depth
    assert card["coverage"] == {
        k: v for k, v in coverage.items() if k != "verification"
    }
    assert "deeper" not in card  # None at the ceiling is left out
    assert card["constraints"] == [RATIONALE_ITEMS[0]]


def test_mcp_linked_rationale_cached_only_miss_is_the_not_cached_shape(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    fake.responses["rationale"] = (
        200,
        {
            "status": "not_cached",
            "target": {"path": "a.py", "line_start": 3, "line_end": 5},
            "project": fake.projects[SLUG],
        },
    )
    with use_project(linked):
        card = whygraph_rationale_brief(qualified_name="pkg.mixed", intent="review")
    assert bodies(fake, "rationale")[0]["intent"] == "review"
    assert card["status"] == "not_cached"
    assert (card["target"]["line_start"], card["target"]["line_end"]) == (3, 5)
    assert "purpose" not in card and "constraints" not in card
    # As the local tool: no plans beside a cached_only miss.
    assert "plans" not in card and plan_requests(fake) == []


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

    order = [item["sha"] for item in payload["evidence"]]
    assert order == [UNCOMMITTED_SHA, repo.c2, repo.c1, repo.c3]
    # The naive string sort would have swapped the middle pair.
    stamps = [item["date"] for item in payload["evidence"]]
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
    assert payload["platform"] == {
        "status": "unreachable",
        "url": API_ORIGIN,
        "answered": False,  # the labels above are local guesses
    }
    assert statuses(payload) == {
        repo.c1: PUSH_PENDING_SCAN,
        repo.c2: PUSH_PENDING_SCAN,
        repo.c3: PUSH_NOT_PUSHED,
        UNCOMMITTED_SHA: PUSH_UNCOMMITTED,
    }
    assert set(sources(payload).values()) == {"local"}

    # A card that times out is a slow platform, not an outage (plan 49, I4).
    with use_project(linked), pytest.raises(WhyGraphError) as slow:
        whygraph_rationale_brief(path="a.py", line_start=1, line_end=6)
    assert "still working" in str(slow.value)

    for call in (
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
    assert payload["platform"] == {
        "status": "revoked",
        "url": API_ORIGIN,
        "answered": False,
    }
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
    assert [i["sha"] for i in payload["evidence"]] == [repo.c1]
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
    db = linked.config.whygraph_db
    assert db is not None

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a linked project must not reach this")

    monkeypatch.setattr(
        "whygraph.mcp.evidence.backfill_evidence_descriptions", forbidden
    )
    monkeypatch.setattr("whygraph.mcp.rationale.lookup_card", forbidden)
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
    assert not (repo.root / ".whygraph" / "whygraph.db").exists()


def test_engine_opens_normally_without_a_remote(repo: SimpleNamespace) -> None:
    """The control: the same context without a ``remote`` still opens its DB."""
    db = project_db_path(portal_db.data_dir(), "local", "demo")
    plain = ProjectContext(slug="demo", root=repo.root, config=Config(whygraph_db=db))
    with use_project(plain):
        assert get_engine() is not None
    assert db.parent.is_dir()


# ---------------------------------------------------------------------------
# Plans (plan 61 section 4.8, section 9.2 case 14)
# ---------------------------------------------------------------------------


def plan_requests(fake: FakePlatform) -> list[httpx.Request]:
    """The plans requests the fake saw (``plans/for`` and the two reads)."""
    return [r for r in fake.requests if "/plans" in r.url.path]


def meta_requests(fake: FakePlatform) -> list[httpx.Request]:
    return [r for r in fake.requests if r.url.path == "/api/v1/meta"]


def answer_plans(fake: FakePlatform, refs: list[dict]) -> None:
    """Make ``POST /plans/for`` answer with ``refs``."""
    fake.responses["plans_for"] = (200, {"plans": refs, "project": fake.projects[SLUG]})


def answer_history(fake: FakePlatform, items: list[dict]) -> None:
    fake.responses["history"] = (
        200,
        {
            "path": "a.py",
            "include_renames": True,
            "evidence": items,
            "project": fake.projects[SLUG],
        },
    )


def plan_ref(stem: str, link: str = "commit", **extra: object) -> dict:
    """One full-detail ref as a platform sends it."""
    return {
        "stem": stem,
        "title": f"Plan {stem}",
        "status": "shipped",
        "link": link,
        "uri": f"whygraph://plan/{stem}",
        "path": f"docs/plans/{stem}.md",
        "present": True,
        "commits": ["a" * 8],
        **extra,
    }


def linked_over(repo: SimpleNamespace, fake: FakePlatform) -> ProjectContext:
    """A linked context over ``repo`` whose platform is ``fake``."""
    fake.add_token()
    remote = LinkedProject(
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
    return ProjectContext(
        slug="demo",
        root=repo.root,
        config=Config(
            whygraph_db=project_db_path(portal_db.data_dir(), "local", "demo"),
            codegraph_db=repo.root / ".codegraph" / "codegraph.db",
        ),
        remote=remote,
    )


def test_mcp_linked_plans_never_asked_by_evidence(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """``whygraph_evidence_for`` carries no plans (R22): no plans call, no meta."""
    answer_evidence(fake, [platform_item(repo.c1, committed_at=C1_DATE)], [repo.c2])
    with use_project(linked):
        payload = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)
    assert "plans" not in payload
    assert plan_requests(fake) == [] and meta_requests(fake) == []


def test_mcp_linked_area_history_plans_send_only_the_platforms_shas(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """File scope: the SHAs of the platform's own answer and the path it was sent."""
    answer_history(
        fake,
        [
            platform_item(repo.c2, committed_at=C2_DATE),
            platform_item(repo.c1, committed_at=C1_DATE),
        ],
    )
    answer_plans(
        fake,
        [plan_ref("built", "file", sections=[3, 8], sections_total=2, hints=[])],
    )
    with use_project(linked):
        payload = whygraph_area_history("a.py", limit=5)

    sent = bodies(fake, "for")
    assert sent == [
        {
            "shas": [repo.c2, repo.c1],
            "paths": ["a.py"],
            "scope": "file",
            "detail": "compact",
            "target": None,
        }
    ]
    raw = json.dumps(sent)
    assert repo.c3 not in raw and UNCOMMITTED_SHA not in raw
    # The compact ref: pointer fields and the sections, nothing of `full`.
    assert payload["plans"] == [
        {
            "stem": "built",
            "title": "Plan built",
            "status": "shipped",
            "link": "file",
            "uri": "whygraph://plan/built",
            "sections": [3, 8],
        }
    ]
    assert payload["platform"] == {"status": "ok", "url": API_ORIGIN}

    fake.requests.clear()
    with use_project(linked):
        full = whygraph_area_history("a.py", limit=5, detail="full")
    assert bodies(fake, "for")[0]["detail"] == "full"
    assert full["plans"][0]["path"] == "docs/plans/built.md"
    assert full["plans"][0]["present"] is True
    assert full["plans"][0]["commits"] == ["a" * 8]
    # The meta read is cached after a success: one per LinkedProject.
    assert meta_requests(fake) == []


def test_mcp_linked_area_history_plans_cut_to_the_file_ceiling(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """At most 5 refs, at most 3 of them ``file`` links, whatever the platform sends."""
    answer_history(fake, [platform_item(repo.c1, committed_at=C1_DATE)])
    answer_plans(
        fake,
        [plan_ref(f"file-{i}", "file") for i in range(5)]
        + [plan_ref(f"commit-{i}") for i in range(4)],
    )
    with use_project(linked):
        payload = whygraph_area_history("a.py")
    assert [ref["stem"] for ref in payload["plans"]] == [
        "file-0",
        "file-1",
        "file-2",
        "commit-0",
        "commit-1",
    ]


def test_mcp_linked_history_of_an_unpushed_path_asks_no_plans(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    (linked.root / "secret.py").write_text("x = 1\n")
    with use_project(linked):
        payload = whygraph_area_history("secret.py")
    assert "plans" not in payload
    assert fake.requests == []


def test_mcp_linked_rationale_plans_send_the_pushed_shas_and_target(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """Symbol scope: first distinct pushed SHAs (blame order) and the sent target."""
    answer_plans(fake, [plan_ref("built", hints=[{"id": "step 2", "n": 4}])])
    with use_project(linked):
        card = whygraph_rationale_brief(path="a.py", line_start=1, line_end=6)
    rationale_body = bodies(fake, "rationale")[0]
    sent = bodies(fake, "for")
    assert sent == [
        {
            # c1 owns lines 1-2 and 5: sent once, in blame order.
            "shas": [repo.c1, repo.c2],
            "paths": ["a.py"],
            "scope": "symbol",
            "detail": "compact",
            "target": {"qualified_name": None, "line_start": 1, "line_end": 6},
        }
    ]
    # Nothing that the card request did not already send.
    assert {h["sha"] for h in rationale_body["hunks"]} == set(sent[0]["shas"])
    assert rationale_body["target"]["path"] == sent[0]["paths"][0]
    raw = json.dumps(sent)
    assert repo.c3 not in raw and UNCOMMITTED_SHA not in raw
    assert card["plans"] == [
        {
            "stem": "built",
            "title": "Plan built",
            "status": "shipped",
            "link": "commit",
            "uri": "whygraph://plan/built",
            "hints": [{"id": "step 2", "n": 4}],
        }
    ]

    fake.requests.clear()
    with use_project(linked):
        whygraph_rationale_brief(qualified_name="pkg.mixed")
    # The declaration line is unpushed, so the name stays here.
    assert bodies(fake, "for")[0]["target"] == {
        "qualified_name": None,
        "line_start": 3,
        "line_end": 5,
    }
    assert bodies(fake, "for")[0]["shas"] == [repo.c2, repo.c1]

    fake.requests.clear()
    with use_project(linked):
        whygraph_rationale_brief(qualified_name="pkg.head")
    assert bodies(fake, "for")[0]["target"] == {
        "qualified_name": "pkg.head",
        "line_start": 1,
        "line_end": 2,
    }


def test_mcp_linked_rationale_plans_need_a_sendable_target_and_a_card(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """No plans call without a pushed hunk, nor after a refused card."""
    with use_project(linked), pytest.raises(WhyGraphError):
        whygraph_rationale_brief(path="a.py", line_start=3, line_end=3)
    assert fake.requests == []

    fake.responses["rationale"] = (
        403,
        error_body("budget_exceeded", PLATFORM_BUDGET_MESSAGE, scope="org"),
    )
    with use_project(linked), pytest.raises(WhyGraphError):
        whygraph_rationale_brief(qualified_name="pkg.head")
    assert plan_requests(fake) == [] and meta_requests(fake) == []


def test_mcp_linked_rationale_plans_send_no_target_for_a_substituted_path(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """A locally renamed file is asked about by its pushed origin path, untargeted."""
    _git(repo.root, "mv", "a.py", "b.py")
    _git(repo.root, "commit", "-q", "-m", "rename locally", date=C3_DATE)
    with use_project(linked):
        whygraph_rationale_brief(path="b.py", line_start=1, line_end=2)
    assert bodies(fake, "rationale")[0]["target"]["path"] == "a.py"
    sent = bodies(fake, "for")
    assert sent == [
        {
            "shas": [repo.c1],
            "paths": ["a.py"],
            "scope": "symbol",
            "detail": "compact",
            "target": None,
        }
    ]
    assert "b.py" not in json.dumps(sent)


def test_mcp_linked_rationale_plans_cut_to_the_card_ceiling(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """5 refs, 4 hints each, a ``file`` link, 300-character titles: cut here."""
    long = "x" * 300
    hints = [{"id": f"step {k}", "title": long, "n": k} for k in range(4)]
    answer_plans(
        fake,
        [plan_ref("only-file", "file", title=long, hints=hints)]
        + [
            plan_ref(f"plan-{i}", "declared", title=long, hints=hints) for i in range(5)
        ],
    )
    with use_project(linked):
        card = whygraph_rationale_brief(path="a.py", line_start=1, line_end=6)
    refs = card["plans"]
    assert 1 <= len(refs) <= 3
    assert all(ref["link"] != "file" for ref in refs)
    assert all(len(ref["title"]) <= 80 for ref in refs)
    for ref in refs:
        assert len(ref.get("hints", [])) <= 2
        assert all(len(h["title"]) <= 80 for h in ref.get("hints", []))
        assert set(ref) <= {"stem", "title", "status", "link", "uri", "hints"}
    assert len(json.dumps(refs, ensure_ascii=False)) <= 1200


def test_mcp_linked_plans_meta_failure_is_not_cached(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """A failed capability read skips plans for one call, then is read again."""
    answer_history(fake, [platform_item(repo.c1, committed_at=C1_DATE)])
    answer_plans(fake, [plan_ref("built")])
    fake.responses["meta"] = (503, error_body("unavailable"))
    with use_project(linked):
        payload = whygraph_area_history("a.py")
    assert "plans" not in payload
    assert payload["platform"] == {"status": "ok", "url": API_ORIGIN}
    assert len(meta_requests(fake)) == 1 and plan_requests(fake) == []

    del fake.responses["meta"]
    with use_project(linked):
        payload = whygraph_area_history("a.py")
    assert [ref["stem"] for ref in payload["plans"]] == ["built"]
    assert len(meta_requests(fake)) == 2 and len(plan_requests(fake)) == 1


def test_mcp_linked_generic_plans_reads_make_no_request(
    linked: ProjectContext, repo: SimpleNamespace, fake: FakePlatform
) -> None:
    """The generic read functions are local-only: no HTTP on a linked context."""
    from whygraph.mcp import plans

    with use_project(linked):
        assert plans.plans_for([repo.c3], ["a.py"], scope="file") == []
        assert plans.guarded_plans_for([repo.c3], ["a.py"], scope="symbol") == []
    assert fake.requests == []


def test_mcp_linked_plans_cut_to_fifty_shas(
    remote: LinkedProject, fake: FakePlatform
) -> None:
    shas = [f"{i + 1:040x}" for i in range(60)]
    refs, supported = remote.plans_for(shas, ["a.py"], scope="file")
    assert (refs, supported) == ([], True)
    assert bodies(fake, "for")[0]["shas"] == shas[:50]


def test_mcp_linked_plan_resources_read_from_the_platform(
    linked: ProjectContext, fake: FakePlatform
) -> None:
    from platform_fake import PLAN_STEM

    from whygraph.mcp.plans import _plan_resource, _plan_section_resource

    with use_project(linked):
        plan = _plan_resource(PLAN_STEM)
        section = _plan_section_resource(PLAN_STEM, "1")
        missing = _plan_resource("nosuch-plan")
        missing_section = _plan_section_resource(PLAN_STEM, "2")
    assert plan["stem"] == PLAN_STEM and plan["digest"]
    assert plan["kind"] == "plan" and plan["reading"]
    assert "project" not in plan and "body" not in plan
    assert plan["platform"] == {"status": "ok", "url": API_ORIGIN}
    assert section["n"] == 1 and section["text"].startswith("## Steps")
    assert "project" not in section
    assert missing == {
        "error": "not_found",
        "stem": "nosuch-plan",
        "platform": {"status": "ok", "url": API_ORIGIN},
    }
    assert missing_section["error"] == "not_found"
    assert missing_section["section"] == "2"


@pytest.mark.parametrize(
    "stem, n",
    [
        ("..", None),
        ("..", "1"),
        ("Bad_Stem", None),
        ("a/b", None),
        ("demo-plan", "x"),
        ("demo-plan", "300"),
        ("demo-plan", "-1"),
    ],
)
def test_mcp_linked_plan_resources_check_locally_first(
    linked: ProjectContext, fake: FakePlatform, stem: str, n: str | None
) -> None:
    """A stem off the pattern, or a section off ``0..299``, never leaves the machine."""
    from whygraph.mcp.plans import _plan_resource, _plan_section_resource

    with use_project(linked):
        payload = _plan_resource(stem) if n is None else _plan_section_resource(stem, n)
    assert payload["error"] == "not_found"
    assert payload["stem"] == stem
    assert payload["platform"]["status"] == "ok"
    assert fake.requests == []


def test_mcp_linked_plans_on_an_older_platform(repo: SimpleNamespace) -> None:
    """No ``plans`` capability: one meta read, no plans request, link ``ok``."""
    from platform_fake import PLAN_STEM

    from whygraph.mcp.plans import _plan_resource, _plan_section_resource

    fake = FakePlatform(plans_supported=False)
    ctx = linked_over(repo, fake)
    answer_history(fake, [platform_item(repo.c1, committed_at=C1_DATE)])
    with use_project(ctx):
        history = whygraph_area_history("a.py")
        card = whygraph_rationale_brief(path="a.py", line_start=1, line_end=6)
        plan = _plan_resource(PLAN_STEM)
        section = _plan_section_resource(PLAN_STEM, "1")
        evidence = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)
    for payload in (history, card):
        assert "plans" not in payload
        assert payload["platform"] == {
            "status": "ok",
            "url": API_ORIGIN,
            "plans": "unsupported",
        }
    # Only the two tool branches say so; the evidence tool's block is unchanged.
    assert "plans" not in evidence["platform"]
    assert plan["error"] == "not_found" and section["error"] == "not_found"
    assert len(meta_requests(fake)) == 1
    assert plan_requests(fake) == []
    assert ctx.remote is not None and ctx.remote.status == "ok"


@pytest.mark.parametrize(
    "answer",
    [(401, error_body("login_required")), (404, error_body("no_such_route"))],
)
def test_mcp_linked_plans_unknown_route_reads_as_unsupported(
    linked: ProjectContext,
    repo: SimpleNamespace,
    fake: FakePlatform,
    answer: tuple[int, dict],
) -> None:
    """An older platform's catch-all answer (or a bare 404) is "no plans here"."""
    answer_history(fake, [platform_item(repo.c1, committed_at=C1_DATE)])
    for route in ("plans_for", "plan", "plan_section"):
        fake.responses[route] = answer
    from platform_fake import PLAN_STEM

    from whygraph.mcp.plans import _plan_resource

    with use_project(linked):
        history = whygraph_area_history("a.py")
        plan = _plan_resource(PLAN_STEM)
    assert "plans" not in history
    assert history["platform"]["plans"] == "unsupported"
    assert plan["error"] == "not_found"
    assert linked.remote is not None and linked.remote.status == "ok"
    with use_project(linked):
        follow = whygraph_evidence_for(path="a.py", line_start=1, line_end=6)
    assert follow["platform"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Notes (plan 63 step 5): linked_propose and linked_notes
# ---------------------------------------------------------------------------
#
# A checkout of its own, so the evidence fixture above stays as pinned:
#
#   pushed (origin/main = p1)     b.py, c.py, wrap.py, a.py, pkg/old.py
#   committed locally (l1)        local.py (new), wrap.py (method wrapped
#                                 in a new class NewLocalWrapper)
#   modified, not committed       a.py
#   untracked                     brand_new_secret.py, b*.py,
#                                 pkg/newsub/inner_secret.py, .env
#   symlink                       link.py -> b.py
#
# The index holds pkg.keep (b.py, fresh), pkg.secret_fn (local.py),
# NewLocalWrapper.method (wrap.py), pkg.ghost_fn (c.py, recorded with bytes
# c.py no longer has: a stale index) and pkg.edited (a.py).

LOCAL_UID = "u-local-secret-uid"


def _node(qn: str, path: str, start: int, end: int) -> dict:
    return {
        "id": f"n_{qn}",
        "kind": "function",
        "name": qn.rsplit(".", 1)[-1],
        "qualified_name": qn,
        "file_path": path,
        "language": "python",
        "start_line": start,
        "end_line": end,
        "docstring": None,
        "signature": None,
    }


NOTE_NODES = [
    _node("pkg.keep", "b.py", 1, 2),
    _node("pkg.secret_fn", "local.py", 1, 2),
    _node("NewLocalWrapper.method", "wrap.py", 2, 3),
    _node("pkg.ghost_fn", "c.py", 1, 2),
    _node("pkg.edited", "a.py", 1, 2),
]


def _note_index(root: Path, *, hashes: dict[str, bytes] | None = None) -> None:
    """(Re)build the index; ``hashes`` overrides a file's recorded bytes.

    ``hashes=None`` records the current bytes of each file; pass ``{}`` and
    no ``content_hash`` column is created at all (an older index).
    """
    import hashlib
    import sqlite3

    db = root / ".codegraph" / "codegraph.db"
    db.parent.mkdir(exist_ok=True)
    if db.exists():
        db.unlink()
    build_fake_codegraph_db(db, nodes=NOTE_NODES, edges=[])
    if hashes == {}:
        return
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
        for path in sorted({n["file_path"] for n in NOTE_NODES}):
            data = (hashes or {}).get(path, (root / path).read_bytes())
            conn.execute(
                "INSERT INTO files (path, language, content_hash) VALUES (?, ?, ?)",
                (path, "python", hashlib.sha256(data).hexdigest()),
            )


@pytest.fixture
def notes_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The checkout drawn above, with no real ``codegraph sync`` possible."""
    root = tmp_path / "notes-checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "remote", "add", "origin", "https://github.com/acme/demo.git")
    files = {
        "b.py": "def keep():\n    return 1\n",
        "c.py": "def plain():\n    return 1\n",
        "wrap.py": "def method():\n    return 1\n",
        "a.py": "def edited():\n    return 1\n",
        "pkg/old.py": "x = 1\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "pushed", date=C1_DATE)
    p1 = _head(root)
    _git(root, "update-ref", "refs/remotes/origin/main", p1)
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    (root / "local.py").write_text("def secret_fn():\n    return 2\n")
    (root / "wrap.py").write_text(
        "class NewLocalWrapper:\n    def method(self):\n        return 1\n"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "local only", date=C2_DATE)
    l1 = _head(root)

    (root / "a.py").write_text("def edited():\n    return 99\n")
    (root / "brand_new_secret.py").write_text("x = 2\n")
    (root / "b*.py").write_text("x = 3\n")
    (root / "pkg" / "newsub").mkdir()
    (root / "pkg" / "newsub" / "inner_secret.py").write_text("x = 4\n")
    (root / ".env").write_text("SECRET=1\n")
    (root / "link.py").symlink_to("b.py")

    _note_index(root, hashes={"c.py": b"def ghost_fn():\n    return 0\n"})
    monkeypatch.setattr(targets_module, "refresh_codegraph_index", lambda *a, **k: None)
    real_which = targets_module.shutil.which
    monkeypatch.setattr(
        targets_module.shutil,
        "which",
        lambda cmd, *a, **k: None if cmd == "codegraph" else real_which(cmd, *a, **k),
    )
    return SimpleNamespace(root=root, p1=p1, l1=l1)


@pytest.fixture
def notes_fake() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def notes_ctx(notes_repo: SimpleNamespace, notes_fake: FakePlatform) -> ProjectContext:
    return linked_over(notes_repo, notes_fake)


def propose(ctx: ProjectContext, body: str = "Keep it simple.", **kw: object) -> dict:
    """``linked_propose`` as the tool will call it, under the linked context."""
    kind = kw.pop("kind", "decision")
    with use_project(ctx), use_actor(LOCAL_UID):
        return dict(linked_propose(ctx.remote, body, kind, **kw))  # type: ignore[arg-type]


def propose_code(ctx: ProjectContext, **kw: object) -> str | None:
    with pytest.raises(WhyGraphError) as caught:
        propose(ctx, **kw)  # type: ignore[arg-type]
    return caught.value.code


def raw(fake: FakePlatform) -> bytes:
    """Every byte the machine sent: each request's URL and body."""
    return b"\n".join(
        str(r.url).encode() + b"\n" + (r.content or b"") for r in fake.requests
    )


def sent_anchor(fake: FakePlatform) -> dict:
    assert len(fake.proposals) == 1
    return fake.proposals[0]["anchor"]


def assert_absent(fake: FakePlatform, *needles: str) -> None:
    data = raw(fake)
    for needle in needles:
        assert needle.encode() not in data, needle


def test_notes_linked_l1_pushed_path(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """L1: a pushed path is sent as given; ``review`` is the platform's page."""
    result = propose(notes_ctx, path="./b.py")
    assert sent_anchor(notes_fake) == {"kind": "path", "path": "b.py"}
    assert result["review"] == f"{API_ORIGIN}/p/{SLUG}/notes?id=7"
    assert result["note_id"] == 7 and result["status"] == "proposed"
    assert result["duplicate"] is False and result["note"]["id"] == 7
    assert result["platform"] == {"status": "ok", "url": API_ORIGIN}
    assert "anchor_downgraded" not in result and "dropped_refs" not in result
    assert_absent(notes_fake, "./b.py")


def test_notes_linked_pushed_directory_path(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """A directory ``D`` tracks is a pushed path too."""
    propose(notes_ctx, path="pkg")
    assert sent_anchor(notes_fake) == {"kind": "path", "path": "pkg"}


def test_notes_linked_l2_range_on_an_identical_file(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """L2: a file byte-identical to ``D`` keeps its range."""
    result = propose(notes_ctx, path="b.py", line_start=1, line_end=2)
    assert sent_anchor(notes_fake) == {
        "kind": "range",
        "path": "b.py",
        "line_start": 1,
        "line_end": 2,
    }
    assert "anchor_downgraded" not in result


def test_notes_linked_l3_range_in_a_modified_file(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """L3: an edited file's range goes as its path, with no lines."""
    result = propose(notes_ctx, path="a.py", line_start=1, line_end=2)
    assert sent_anchor(notes_fake) == {"kind": "path", "path": "a.py"}
    assert b"line_start" not in raw(notes_fake)
    assert result["anchor_downgraded"] == {
        "from": "range",
        "to": "path",
        "reason": "not_pushed",
    }
    # A file changed only by an unpushed commit is the same case.
    notes_fake.proposals.clear()
    propose(notes_ctx, path="wrap.py", line_start=1, line_end=2)
    assert sent_anchor(notes_fake) == {"kind": "path", "path": "wrap.py"}


def test_notes_linked_symbol_in_an_identical_file(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """A symbol whose file is identical to ``D`` and freshly indexed goes by name."""
    result = propose(notes_ctx, qualified_name="  pkg.keep ")
    assert sent_anchor(notes_fake) == {"kind": "symbol", "qualified_name": "pkg.keep"}
    assert "anchor_downgraded" not in result


def test_notes_linked_l4_symbol_in_a_local_only_commit(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """L4: a symbol defined only in an unpushed file goes project-wide, unnamed."""
    result = propose(notes_ctx, qualified_name="pkg.secret_fn")
    assert sent_anchor(notes_fake) == {"kind": "project"}
    assert result["anchor_downgraded"] == {
        "from": "symbol",
        "to": "project",
        "reason": "not_pushed",
    }
    assert_absent(notes_fake, "secret_fn", "local.py")


def test_notes_linked_l5_untracked_file(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """L5: an untracked file (and a glob-shaped name) goes project-wide."""
    propose(notes_ctx, path="brand_new_secret.py")
    propose(notes_ctx, path="b*.py", line_start=1, line_end=1)
    assert [p["anchor"] for p in notes_fake.proposals] == [{"kind": "project"}] * 2
    assert_absent(notes_fake, "brand_new_secret", "b*.py")


def test_notes_linked_l6_refs(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace, notes_fake: FakePlatform
) -> None:
    """L6: an unpushed SHA is dropped, ``#N`` kept, a pushed prefix expanded."""
    unpushed = notes_repo.l1[:10].upper()
    result = propose(
        notes_ctx,
        project_wide=True,
        refs=[unpushed, "#12", notes_repo.p1[:8], "#12", "deadbeefcafe"],
    )
    assert notes_fake.proposals[0]["refs"] == ["#12", notes_repo.p1]
    assert result["dropped_refs"] == [
        {"ref": unpushed.lower(), "reason": "not_pushed"},
        {"ref": "deadbeefcafe", "reason": "not_pushed"},
    ]
    assert_absent(
        notes_fake, notes_repo.l1[:7], notes_repo.l1[:7].upper(), "deadbeefcafe"
    )


@pytest.mark.parametrize("refs", [["main"], ["--output=x"], ["#x"], "abc1234"])
def test_notes_linked_bad_refs_are_refused_locally(
    notes_ctx: ProjectContext, notes_fake: FakePlatform, refs: object
) -> None:
    """A ref that is neither a SHA nor ``#N`` never reaches git or the platform."""
    assert propose_code(notes_ctx, project_wide=True, refs=refs) == "note_invalid"
    assert notes_fake.requests == []


@pytest.mark.parametrize(
    "anchor",
    [
        {"path": ".env"},
        {"path": "link.py"},
        {"path": "link.py", "line_start": 1, "line_end": 1},
        {"path": "B.py"},  # a case variant: refused on every file system
        {"path": "../outside.py"},
        {"path": "b.py", "line_start": 1, "line_end": 99},  # past the end
        {"qualified_name": "pkg.not_indexed"},
        {"path": "b.py", "project_wide": True},
    ],
)
def test_notes_linked_l7_local_refusals_make_no_request(
    notes_ctx: ProjectContext, notes_fake: FakePlatform, anchor: dict
) -> None:
    """L7: a denied, symlinked or otherwise invalid anchor is refused locally."""
    assert propose_code(notes_ctx, **anchor) == "note_invalid"
    assert notes_fake.requests == []


def test_notes_linked_other_local_validation_makes_no_request(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """Body, kind, check and supersedes_id are checked before any request."""
    cases = [
        ({"body": "   "}, "note_invalid"),
        ({"kind": "purpose", "project_wide": True}, "note_invalid"),
        (
            {
                "project_wide": True,
                "check": {"kind": "absent", "text": "abc", "glob": "*.py"},
            },
            "note_check_invalid",  # a check needs a constraint
        ),
        ({"project_wide": True, "supersedes_id": "3"}, "note_invalid"),
        ({"project_wide": True, "supersedes_id": 0}, "note_not_found"),
        ({"area": "x" * 129}, "note_invalid"),
    ]
    for kw, code in cases:
        assert propose_code(notes_ctx, **kw) == code, kw
    assert notes_fake.requests == []


def test_notes_linked_sends_check_and_supersedes(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    propose(
        notes_ctx,
        kind="constraint",
        area=" backend ",
        check={"kind": "absent", "text": "eval(", "glob": "src/**/*.py"},
        supersedes_id=3,
    )
    body = notes_fake.proposals[0]
    assert body == {
        "body": "Keep it simple.",
        "kind": "constraint",
        "anchor": {"kind": "area", "area": "backend"},
        "fallback_to_project": True,
        "refs": [],
        "check": {"kind": "absent", "text": "eval(", "glob": "src/**/*.py"},
        "supersedes_id": 3,
    }


def test_notes_linked_unsupported_platform(notes_repo: SimpleNamespace) -> None:
    """An older platform: ``note_unsupported`` after ``meta``, never a POST."""
    fake = FakePlatform(notes_supported=False)
    ctx = linked_over(notes_repo, fake)
    assert propose_code(ctx, project_wide=True) == "note_unsupported"
    assert [r.url.path for r in fake.requests] == ["/api/v1/meta"]
    assert fake.proposals == []
    assert ctx.remote is not None and ctx.remote.status == "ok"


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        ((429, "note_limit"), "note_limit"),
        ((400, "note_invalid"), "note_invalid"),
        ((409, "note_state"), "note_state"),
        ((404, "note_not_found"), "note_not_found"),
        ((429, "throttled"), "throttled"),
        ((400, "note_made_up"), None),  # an unknown code is never passed through
        ((403, "forbidden"), None),
    ],
)
def test_notes_linked_l9_platform_codes(
    notes_ctx: ProjectContext,
    notes_fake: FakePlatform,
    refusal: tuple[int, str],
    code: str | None,
) -> None:
    """L9: the platform's note codes keep their code; the link stays ``ok``."""
    notes_fake.notes_refusal = refusal
    assert propose_code(notes_ctx, project_wide=True) == code
    assert notes_ctx.remote is not None and notes_ctx.remote.status == "ok"


@pytest.mark.parametrize("failure", ["unreachable", "timeout", "connect_timeout"])
def test_notes_linked_l10_unreachable(
    notes_ctx: ProjectContext, notes_fake: FakePlatform, failure: str
) -> None:
    """L10: after one good call, an outage is the link's error, and nothing is queued."""
    propose(notes_ctx, project_wide=True)
    notes_fake.failure = failure
    with pytest.raises(WhyGraphError) as caught:
        propose(notes_ctx, project_wide=True)
    assert caught.value.code is None
    assert notes_ctx.remote is not None
    assert notes_ctx.remote.status == "unreachable"
    assert "platform unreachable" in str(caught.value)
    assert len(notes_fake.proposals) == 1
    assert TOKEN not in str(caught.value)


def test_notes_linked_l13_platform_downgrade_passes_through(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace, notes_fake: FakePlatform
) -> None:
    """L13: ``not_on_platform`` from the platform; a local downgrade wins the reason."""
    notes_fake.propose_reply = {
        "note_id": 9,
        "status": "proposed",
        "duplicate": False,
        "anchor_downgraded": {
            "from": "path",
            "to": "project",
            "reason": "not_on_platform",
        },
        "dropped_refs": [{"ref": notes_repo.p1, "reason": "not_on_platform"}],
    }
    result = propose(notes_ctx, path="b.py", refs=[notes_repo.p1])
    assert result["anchor_downgraded"] == {
        "from": "path",
        "to": "project",
        "reason": "not_on_platform",
    }
    assert result["dropped_refs"] == [
        {"ref": notes_repo.p1, "reason": "not_on_platform"}
    ]
    assert result["review"] == f"{API_ORIGIN}/p/{SLUG}/notes?id=9"
    assert "note" not in result

    # Both sides narrowed: symbol -> path here, path -> project there.
    result = propose(
        notes_ctx, qualified_name="NewLocalWrapper.method", refs=[notes_repo.l1]
    )
    assert result["anchor_downgraded"] == {
        "from": "symbol",
        "to": "project",
        "reason": "not_pushed",
    }
    assert result["dropped_refs"] == [
        {"ref": notes_repo.l1, "reason": "not_pushed"},
        {"ref": notes_repo.p1, "reason": "not_on_platform"},
    ]


def test_notes_linked_l14_stale_index(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace, notes_fake: FakePlatform
) -> None:
    """L14: a stale index never names its symbol, even in a file identical to ``D``."""
    result = propose(notes_ctx, qualified_name="pkg.ghost_fn")
    assert sent_anchor(notes_fake) == {"kind": "path", "path": "c.py"}
    assert result["anchor_downgraded"]["reason"] == "not_pushed"
    assert_absent(notes_fake, "ghost_fn")


def test_notes_linked_l14_index_without_hashes(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace, notes_fake: FakePlatform
) -> None:
    """An index that recorded no file hashes cannot prove freshness: no name."""
    _note_index(notes_repo.root, hashes={})
    propose(notes_ctx, qualified_name="pkg.keep")
    assert sent_anchor(notes_fake) == {"kind": "path", "path": "b.py"}
    assert_absent(notes_fake, "pkg.keep")


def test_notes_linked_l15_method_wrapped_in_a_new_class(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """L15: the new class name never leaves; the pushed file's path does."""
    result = propose(notes_ctx, qualified_name="NewLocalWrapper.method")
    assert sent_anchor(notes_fake) == {"kind": "path", "path": "wrap.py"}
    assert result["anchor_downgraded"] == {
        "from": "symbol",
        "to": "path",
        "reason": "not_pushed",
    }
    assert_absent(notes_fake, "NewLocalWrapper")


def test_notes_linked_l15_symbol_in_a_modified_file(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """A freshly indexed symbol in an uncommitted edit goes as its path."""
    propose(notes_ctx, qualified_name="pkg.edited")
    assert sent_anchor(notes_fake) == {"kind": "path", "path": "a.py"}
    assert_absent(notes_fake, "pkg.edited")


@pytest.mark.parametrize(
    "path", ["pkg/newsub", "pkg/newsub/inner_secret.py", "pkg/newsub/"]
)
def test_notes_linked_l16_new_subdirectory_under_a_pushed_one(
    notes_ctx: ProjectContext, notes_fake: FakePlatform, path: str
) -> None:
    """L16: a new local directory (or a file in it) under ``pkg/`` goes project-wide."""
    result = propose(notes_ctx, path=path)
    assert sent_anchor(notes_fake) == {"kind": "project"}
    assert result["anchor_downgraded"]["to"] == "project"
    assert_absent(notes_fake, "newsub", "inner_secret")


@pytest.mark.parametrize(
    "kw",
    [
        {"body": "use ghp_" + "a1B2c3D4" * 3 + " here", "project_wide": True},
        {"body": "token wgc_" + "A1b2C3d4" * 3, "project_wide": True},
        {"body": "key sk-" + "Z9y8X7w6" * 3, "project_wide": True},
        {"area": "sk-" + "q" * 24},
        {
            "kind": "constraint",
            "project_wide": True,
            "check": {"kind": "absent", "text": "ghs_" + "x" * 12, "glob": "*.py"},
        },
        {
            "kind": "constraint",
            "project_wide": True,
            "check": {"kind": "absent", "text": "abc", "glob": "gho_aaaaaaaaaa/*.py"},
        },
    ],
)
def test_notes_linked_l17_token_text_is_refused(
    notes_ctx: ProjectContext, notes_fake: FakePlatform, kw: dict
) -> None:
    """L17: token-shaped free text is refused locally, with no request."""
    assert propose_code(notes_ctx, **kw) == "note_invalid"
    assert notes_fake.requests == []


def test_notes_linked_l18_no_identity_on_the_wire(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace, notes_fake: FakePlatform
) -> None:
    """L18: no origin, proposer, actor or local uid in any request body."""
    propose(notes_ctx, path="b.py", line_start=1, line_end=2, refs=["#3"])
    propose(notes_ctx, qualified_name="pkg.keep")
    propose(notes_ctx, project_wide=True)
    for body in notes_fake.proposals:
        assert set(body) <= {
            "body",
            "kind",
            "anchor",
            "fallback_to_project",
            "refs",
            "check",
            "supersedes_id",
        }
    for request in notes_fake.requests:
        content = request.content or b""
        for needle in (b'"origin"', b"proposed_by", b"actor", LOCAL_UID.encode()):
            assert needle not in content
    assert_absent(notes_fake, LOCAL_UID, str(notes_repo.root))


def test_notes_linked_without_a_default_branch(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace, notes_fake: FakePlatform
) -> None:
    """No ``D``: every anchor but project and area is sent as project."""
    _git(notes_repo.root, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    _git(notes_repo.root, "update-ref", "-d", "refs/remotes/origin/main")
    asked = [
        {"path": "b.py"},
        {"path": "b.py", "line_start": 1, "line_end": 2},
        {"qualified_name": "pkg.keep"},
    ]
    for kw in asked:
        result = propose(notes_ctx, refs=[notes_repo.p1, "#4"], **kw)
        assert result["anchor_downgraded"]["to"] == "project"
        assert result["dropped_refs"] == [
            {"ref": notes_repo.p1, "reason": "not_pushed"}
        ]
    propose(notes_ctx, area="backend")
    propose(notes_ctx, project_wide=True)
    assert [p["anchor"] for p in notes_fake.proposals] == [{"kind": "project"}] * 3 + [
        {"kind": "area", "area": "backend"},
        {"kind": "project"},
    ]
    assert all(p["refs"] == ["#4"] for p in notes_fake.proposals[:3])
    assert_absent(notes_fake, "pkg.keep", "b.py", notes_repo.p1)


# -- linked_notes --------------------------------------------------------------


def notes_for_bodies(fake: FakePlatform) -> list[dict]:
    return [
        json.loads(request.content)
        for request in fake.requests
        if request.url.path.endswith("/notes/for")
    ]


def _target(path: str, qn: str | None = None) -> Target:
    return Target(path=path, line_start=1, line_end=2, qualified_name=qn)


def test_linked_notes_asks_only_what_was_sendable(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace, notes_fake: FakePlatform
) -> None:
    repo = Repository(notes_repo.root)
    remote = notes_ctx.remote
    with use_project(notes_ctx):
        same = linked_notes(remote, repo, _target("b.py", "pkg.keep"), _target("b.py"))
        other = linked_notes(
            remote, repo, _target("moved.py", "pkg.x"), _target("b.py", "pkg.x")
        )
        tracked = linked_notes(remote, repo, _target("c.py", "pkg.c"), None)
        local = linked_notes(remote, repo, _target("local.py", "pkg.secret_fn"), None)
    assert same[1] and other[1] and tracked[1]
    assert same[0] == notes_fake.notes
    assert local == (None, True)
    assert notes_for_bodies(notes_fake) == [
        {"path": "b.py", "line_start": 1, "line_end": 2},
        {"path": "b.py"},
        {"path": "c.py"},
    ]
    assert_absent(notes_fake, "moved.py", "local.py", "secret_fn", "pkg.c")


def test_linked_notes_never_raises(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace, notes_fake: FakePlatform
) -> None:
    repo = Repository(notes_repo.root)
    notes_fake.failure = "unreachable"
    with use_project(notes_ctx):
        assert linked_notes(
            notes_ctx.remote, repo, _target("b.py"), _target("b.py")
        ) == (
            None,
            True,
        )


def test_linked_notes_on_an_older_platform(notes_repo: SimpleNamespace) -> None:
    fake = FakePlatform(notes_supported=False)
    ctx = linked_over(notes_repo, fake)
    with use_project(ctx):
        answer = linked_notes(
            ctx.remote, Repository(notes_repo.root), _target("b.py"), _target("b.py")
        )
    assert answer == ([], False)
    assert notes_for_bodies(fake) == []


# -- the tool, card and resource (plan 63 step 6) --------------------------------


def _registered_tools() -> dict:
    import asyncio

    from whygraph.mcp import server as mcp_server

    return {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}


def _call_tool(name: str, args: dict) -> dict:
    """Call a registered tool through the MCP server; its structured result."""
    import asyncio

    from whygraph.mcp import server as mcp_server

    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    if isinstance(result, tuple):  # (content, structured)
        return result[1]
    return result.structuredContent


def _valid(name: str, result: dict) -> None:
    import jsonschema

    jsonschema.validate(
        json.loads(json.dumps(result, default=str)),
        _registered_tools()[name].outputSchema,
    )


def brief(ctx: ProjectContext, **kw: object) -> dict:
    from whygraph.mcp.rationale import _rationale_brief_tool

    with use_project(ctx):
        return dict(_rationale_brief_tool(**kw))  # type: ignore[arg-type]


def test_mcp_linked_propose_tool_forwards_to_the_platform(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """The registered tool routes a linked project to ``linked_propose`` (L19)."""
    with use_project(notes_ctx), use_actor(LOCAL_UID):
        result = _call_tool(
            "whygraph_propose_note",
            {"body": "Keep it simple.", "kind": "decision", "path": "b.py"},
        )
    assert notes_fake.proposals[0]["anchor"] == {"kind": "path", "path": "b.py"}
    assert result["note_id"] == 7 and result["review"].startswith(API_ORIGIN)
    assert result["platform"] == {"status": "ok", "url": API_ORIGIN}
    _valid("whygraph_propose_note", result)


def test_mcp_linked_propose_result_validates_with_every_optional_key(
    notes_ctx: ProjectContext, notes_repo: SimpleNamespace
) -> None:
    """L19: ``anchor_downgraded`` and ``dropped_refs`` fit the output schema."""
    result = propose(notes_ctx, path="local.py", refs=[notes_repo.l1, "#3"])  # type: ignore[arg-type]
    assert "anchor_downgraded" in result and "dropped_refs" in result
    _valid("whygraph_propose_note", result)


def test_mcp_linked_card_carries_the_platforms_notes(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """L11: a pushed path gets the card plus the platform's notes (L19 schema)."""
    card = brief(notes_ctx, path="b.py", line_start=1, line_end=2)
    assert [n["id"] for n in card["notes"]] == [1]
    assert "unsupported" not in json.dumps(card["platform"])
    assert notes_for_bodies(notes_fake) == [
        {"path": "b.py", "line_start": 1, "line_end": 2}
    ]
    _valid("whygraph_rationale_brief", card)


def test_mcp_linked_card_notes_ride_a_not_cached_miss(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    notes_fake.responses["rationale"] = (
        200,
        {
            "status": "not_cached",
            "target": {"path": "b.py", "line_start": 1, "line_end": 2},
            "project": notes_fake.projects[SLUG],
        },
    )
    card = brief(notes_ctx, path="b.py", line_start=1, line_end=2, cached_only=True)
    assert card["status"] == "not_cached" and card["notes"]


def test_mcp_linked_no_evidence_with_notes_on_a_pushed_path(
    notes_ctx: ProjectContext, notes_fake: FakePlatform, monkeypatch
) -> None:
    """L11: no pushed hunks but a path ``D`` tracks - notes only, path only."""
    from whygraph.mcp import rationale

    def no_evidence(*a: object, **k: object) -> None:
        raise rationale.NoEvidenceError("none", code="no_evidence")

    monkeypatch.setattr(rationale, "_linked_rationale", no_evidence)
    reply = brief(notes_ctx, path="c.py", line_start=1, line_end=2)
    assert reply["status"] == "no_evidence" and reply["notes"]
    assert reply["target"]["path"] == "c.py"
    assert reply["platform"] == {"status": "ok", "url": API_ORIGIN}
    assert notes_for_bodies(notes_fake) == [{"path": "c.py"}]
    _valid("whygraph_rationale_brief", reply)


def test_mcp_linked_no_evidence_without_notes_reraises(
    notes_ctx: ProjectContext, notes_fake: FakePlatform, monkeypatch
) -> None:
    """L11: an unpushed path asks nothing and keeps the original refusal."""
    with pytest.raises(WhyGraphError) as caught:
        brief(notes_ctx, path="local.py", line_start=1, line_end=1)
    assert caught.value.code == "no_evidence"
    assert notes_for_bodies(notes_fake) == []
    from whygraph.mcp import rationale

    def no_evidence(*a: object, **k: object) -> None:
        raise rationale.NoEvidenceError("none", code="no_evidence")

    monkeypatch.setattr(rationale, "_linked_rationale", no_evidence)
    notes_fake.notes = []
    with pytest.raises(WhyGraphError) as again:
        brief(notes_ctx, path="c.py", line_start=1, line_end=2)
    assert again.value.code == "no_evidence"


def test_mcp_linked_card_notes_on_a_substituted_path_send_the_path_only(
    notes_ctx: ProjectContext, notes_fake: FakePlatform, monkeypatch
) -> None:
    """L11: a ``sendable`` on another path never leaks the target's name."""
    from whygraph.mcp import rationale

    sendable = Target(path="b.py", line_start=1, line_end=2, qualified_name="pkg.x")

    def substituted(remote: object, target: Target, **k: object) -> tuple:
        return {"purpose": "p", "target": {}, "platform": {}}, sendable

    monkeypatch.setattr(rationale, "_linked_rationale", substituted)
    card = brief(notes_ctx, path="moved.py", line_start=1, line_end=2)
    assert card["notes"]
    assert notes_for_bodies(notes_fake) == [{"path": "b.py"}]
    assert_absent(notes_fake, "moved.py", "pkg.x")


def test_mcp_linked_card_on_an_older_platform_says_notes_are_unsupported(
    notes_repo: SimpleNamespace,
) -> None:
    """L8 (card half): ``platform.notes == "unsupported"``, no notes key."""
    fake = FakePlatform(notes_supported=False)
    ctx = linked_over(notes_repo, fake)
    card = brief(ctx, path="b.py", line_start=1, line_end=2)
    assert card["platform"]["notes"] == "unsupported"
    assert "notes" not in card
    _valid("whygraph_rationale_brief", card)


def test_mcp_linked_public_brief_stays_note_free(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    with use_project(notes_ctx):
        card = whygraph_rationale_brief(path="b.py", line_start=1, line_end=2)
    assert "notes" not in card
    assert notes_for_bodies(notes_fake) == []


def test_mcp_linked_note_resource_reads_from_the_platform(
    notes_ctx: ProjectContext, notes_fake: FakePlatform
) -> None:
    """L12: the platform's confirmed note, a miss, and a non-int id."""
    from whygraph.mcp.notes import _note_resource

    with use_project(notes_ctx):
        served = _note_resource(1)
        assert served["id"] == 1 and served["body"] == "Keep the demo offline."
        assert served["platform"] == {"status": "ok", "url": API_ORIGIN}
        missing = _note_resource(99)
        assert missing["error"] == "not_found" and missing["id"] == 99
        before = len(notes_fake.requests)
        refused = _note_resource("abc")  # type: ignore[arg-type]
        assert refused["error"] == "not_found"
        assert len(notes_fake.requests) == before
