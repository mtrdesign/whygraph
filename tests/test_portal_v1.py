"""The platform's ``/api/v1`` data routes and the org limits (M2e plan section 4.5).

Layer 1 of the testing plan: a real production portal (``prod_portal``), raw
bearer headers, and the ``api`` project of org ``acme`` over a **real** git
repository whose WhyGraph DB is filled by a **real** ``GitCrawler`` run - so
``/evidence``, ``/rationale`` and ``/history`` answer from genuine blame and
genuine commit rows. Layer 4 (cross-mode evidence in one process) rides along
in :func:`test_v1_evidence_after_uncommitted_edit_matches_local_project`: the
local side's ``collect_evidence`` and the route's ``evidence_from_hunks`` run
against the same repository in the same process.

Only the LLM is stubbed (``RationaleGenerator`` / the description backfill),
the way ``tests/test_mcp_rationale.py`` and ``tests/test_analyze_backfill.py``
do.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, `production_env`) are imported

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from rich.progress import Progress
from sqlmodel import select

from test_portal_app import (  # noqa: F401 -- fixtures
    GITHUB_FAKE_URL,
    V1_ROUTES,
    at,
    claim_instance,
    env,
    manual_ctx,
    portal_client,
    prod_portal,
    production_env,
    seed_codegraph,
    signed_in,
)
from test_portal_hosts_isolation import _insert_project
from whygraph.analyze import AnalyzeError, Rationale
from whygraph.api_v1 import (
    EvidenceReplyOut,
    RationaleOut,
    StatusOut,
    hunk_in_from_blame,
)
from whygraph.core.context import use_project
from whygraph.db import get_session
from whygraph.db.models import Commit
from whygraph.mcp import rationale as rationale_mod
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.evidence import collect_evidence
from whygraph.mcp.rationale import NoEvidenceError
from whygraph.mcp.targets import Target
from whygraph.portal import connections
from whygraph.portal import db as portal_db
from whygraph.portal import orgs
from whygraph.portal import policy, v1_routes
from whygraph.portal.config_layers import load_layer, save_layer
from whygraph.portal.models import Organization, Project, User
from whygraph.portal.v1_routes import GIT_BUDGET, MAX_BODY_BYTES, org_limit
from whygraph.scan.git_crawler import GitCrawler
from whygraph.services.git import GitError, Repository
from whygraph.services.llm import LlmError

SLUG = "api"
ORG = "acme"
ZERO_SHA = "0" * 40


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------


@dataclass
class V1World:
    """A production portal with one crawled project and one bearer token."""

    client: TestClient
    root: Path
    org_id: int
    project_id: int
    ids: dict[str, int] = field(default_factory=dict)
    token: str = ""

    @property
    def state(self):  # noqa: ANN201
        return self.client.app.state.portal

    def ctx(self):
        """A manual context over the project's own DB paths (in-process reads)."""
        return manual_ctx(self.root, slug=SLUG)


def _git(root: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when is not None:
        env |= {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout


def make_dated_repo(parent: Path, name: str = SLUG) -> Path:
    """``make_repo``'s two-commit ``sample.py``, at **fixed** commit dates.

    The dates (and so the SHAs) are constant, which keeps the area-history
    ordering - and the recorded ``/api/v1`` fixtures - deterministic;
    ``make_repo`` stamps "now", so two commits can share a second and the
    tie's order is then the database's whim.
    """
    root = parent / name
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "sample.py").write_text("line one\nline two\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-q", "-m", "first commit", when="2026-01-01T00:00:00+00:00")
    (root / "sample.py").write_text("line one\nline two\nline three\n")
    _git(root, "add", "sample.py")
    _git(root, "commit", "-q", "-m", "second commit", when="2026-01-02T00:00:00+00:00")
    return Path(os.path.realpath(root))


def _mint(user_id: int, project_id: int, name: str = "laptop") -> str:
    with portal_db.get_session() as session:
        return connections.issue(
            session,
            user=session.get(User, user_id),
            project=session.get(Project, project_id),
            client_name=name,
        )


@pytest.fixture
def v1(production_env: Any, tmp_path: Path) -> Iterator[V1World]:
    """Ada claims the instance; Ben owns ``acme``, which holds ``api``.

    ``api``'s root is a real two-commit repository with a seeded CodeGraph
    index (``sample.fn`` at ``sample.py`` lines 1-3) and a WhyGraph DB filled
    by a real ``GitCrawler`` run. Cy is a member and Fay
    an admin, so the owner-only org settings can be checked.
    """
    root = make_dated_repo(tmp_path / "roots")
    with prod_portal() as client:
        claim_instance(client)
        client.cookies.clear()
        ids: dict[str, int] = {}
        with portal_db.get_session() as session:
            for n, login in enumerate(("ben", "cy", "fay")):
                user = User(
                    display_name=login.title(),
                    github_id=6100 + n,
                    github_login=login,
                )
                session.add(user)
                session.flush()
                ids[login] = user.id
            org_id = orgs.create_org(session, slug=ORG, name="Acme").id
            for login, role in (("ben", "owner"), ("cy", "member"), ("fay", "admin")):
                orgs.add_member(session, org_id=org_id, user_id=ids[login], role=role)
        project_id = _insert_project(
            org_id,
            SLUG,
            "Api project",
            root,
            ids["ben"],
            github=(70, 7000),
            remote_url=f"{GITHUB_FAKE_URL}/{ORG}/{SLUG}",
            default_branch="main",
        )
        seed_codegraph(root)
        world = V1World(client, root, org_id, project_id, ids)
        with use_project(world.ctx()):
            GitCrawler(Progress(), repository=Repository(root)).run()
        world.token = _mint(ids["ben"], project_id)
        yield world


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def call(
    w: V1World,
    method: str,
    tail: str = "",
    *,
    token: str | None = None,
    org: str = ORG,
    slug: str = SLUG,
    **kwargs: Any,
) -> httpx.Response:
    """One ``/api/v1`` request with a bearer header and no cookie."""
    w.client.cookies.clear()
    raw = w.token if token is None else token
    headers = {"Authorization": f"Bearer {raw}"} if raw else {}
    return w.client.request(
        method, f"{at(org)}/api/v1/projects/{slug}{tail}", headers=headers, **kwargs
    )


def target_body(
    path: str = "sample.py",
    start: int = 1,
    end: int = 3,
    name: str | None = None,
) -> dict:
    body = {"path": path, "line_start": start, "line_end": end}
    if name is not None:
        body["qualified_name"] = name
    return body


def hunks_of(w: V1World, start: int = 1, end: int = 3) -> list[dict]:
    """The committed blame hunks of the repository's own working tree, on the wire."""
    with use_project(w.ctx()):
        blamed = Repository(w.root).blame("sample.py", start, end)
    return [
        hunk_in_from_blame(hunk).model_dump()
        for hunk in blamed
        if set(hunk.sha) != {"0"}
    ]


def _target(start: int, end: int, name: str | None = None) -> Target:
    """The in-process :class:`Target` the route's body would build."""
    return Target(path="sample.py", line_start=start, line_end=end, qualified_name=name)


def labels(items: Sequence[dict]) -> list[tuple[str, str]]:
    return [(item["commit"]["sha"], item["source"]) for item in items]


class _Generator:
    """Stub :class:`~whygraph.analyze.RationaleGenerator`; counts generations."""

    calls = 0

    @classmethod
    def from_config(cls, config: object) -> _Generator:
        return cls()

    def generate(self, evidence, *, symbol_context=None) -> Rationale:  # noqa: ANN001
        type(self).calls += 1
        return Rationale(
            purpose="Holds the sample lines.",
            why="Two commits built it up.",
            constraints=("keep it small",),
            tradeoffs=(),
            risks=(),
            model="fake-1",
            provider="fake",
        )


@pytest.fixture
def generator(monkeypatch: pytest.MonkeyPatch) -> type[_Generator]:
    """``RationaleGenerator`` stubbed; ``generator.calls`` counts generations."""
    _Generator.calls = 0
    monkeypatch.setattr(rationale_mod, "RationaleGenerator", _Generator)
    # A configured, reachable provider: the key pre-check is its own test.
    monkeypatch.setattr(v1_routes, "_missing_key", lambda *a, **k: None)
    return _Generator


def set_defaults(w: V1World, config: dict, *, who: str = "ben") -> httpx.Response:
    """``PUT /api/portal/defaults`` as ``who`` (the org layer)."""
    w.client.cookies.clear()
    signed_in(w.client, w.ids[who])
    response = w.client.put(f"{at(ORG)}/api/portal/defaults", json={"config": config})
    w.client.cookies.clear()
    return response


# ---------------------------------------------------------------------------
# Evidence (plan section 4.5, 5.3 "v1 data")
# ---------------------------------------------------------------------------


def test_v1_evidence_matches_local_evidence(v1: V1World) -> None:
    """The route's bundle equals what ``collect_evidence`` builds locally."""
    with use_project(v1.ctx()):
        local = collect_evidence(_target(1, 3))
    assert local, "the crawled repo must yield evidence"

    response = call(
        v1, "POST", "/evidence", json={"target": target_body(), "hunks": hunks_of(v1)}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    reply = EvidenceReplyOut.model_validate(body)
    assert labels(body["evidence"]) == [(e.commit.sha, e.source) for e in local]
    assert reply.unknown_shas == []
    assert StatusOut.model_validate(body["project"]).slug == SLUG


def test_v1_evidence_after_uncommitted_edit_matches_local_project(
    v1: V1World,
) -> None:
    """Layer 4: an uncommitted edit changes neither side's answer.

    The developer's blame of a dirty working tree yields one uncommitted hunk
    (dropped before the request) plus the pushed ones; the platform answers
    from the pushed hunks alone and matches the local collector.
    """
    (v1.root / "sample.py").write_text("line one\nline two\nline three\nedited\n")
    with use_project(v1.ctx()):
        blamed = Repository(v1.root).blame("sample.py", 1, 4)
        local = collect_evidence(_target(1, 4))
    assert any(set(hunk.sha) == {"0"} for hunk in blamed), "the edit is uncommitted"

    pushed = [
        hunk_in_from_blame(hunk).model_dump()
        for hunk in blamed
        if set(hunk.sha) != {"0"}
    ]
    response = call(
        v1,
        "POST",
        "/evidence",
        json={"target": target_body(end=4), "hunks": pushed},
    )
    assert response.status_code == 200, response.text
    assert labels(response.json()["evidence"]) == [
        (e.commit.sha, e.source) for e in local
    ]


def test_v1_evidence_reports_unknown_shas(v1: V1World) -> None:
    """A pushed SHA the platform has never scanned comes back labelled."""
    unknown = "b" * 40
    response = call(
        v1,
        "POST",
        "/evidence",
        json={
            "target": target_body(),
            "hunks": [
                {
                    "sha": unknown,
                    "origins": [{"path": "sample.py", "start": 1, "end": 1}],
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["unknown_shas"] == [unknown]


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        ("sha", "--upload-pack=touch"),
        ("sha", ZERO_SHA),
        ("sha", "HEAD"),
        ("sha", "A" * 40),
        ("path", "-o/tmp/pwn"),
        ("path", "/etc/passwd"),
        ("path", "../outside.py"),
        ("path", "ok\nnewline"),
    ],
)
def test_v1_rejects_option_like_sha_and_path(
    v1: V1World, field_path: str, value: str
) -> None:
    """Nothing option-like reaches ``git blame``'s argv (mutation #11)."""
    origin = {"path": "sample.py", "start": 1, "end": 1}
    hunk: dict[str, Any] = {"sha": "a" * 40, "origins": [origin]}
    if field_path == "sha":
        hunk["sha"] = value
    else:
        origin["path"] = value
    response = call(
        v1, "POST", "/evidence", json={"target": target_body(), "hunks": [hunk]}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "bad_request"
    assert value not in response.text


@pytest.mark.parametrize("sha", ["--help", "nothex", "a" * 39, ZERO_SHA])
def test_v1_commit_resource_rejects_an_unsafe_sha(v1: V1World, sha: str) -> None:
    response = call(v1, "GET", f"/commits/{sha}")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "bad_request"


@pytest.mark.parametrize("path", ["-o/tmp/pwn", "/etc/passwd", "../outside.py"])
def test_v1_history_rejects_an_unsafe_path(v1: V1World, path: str) -> None:
    response = call(v1, "GET", "/history", params={"path": path})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "bad_request"


def test_v1_hides_git_stderr(v1: V1World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``GitError`` becomes ``422 bad_hunk`` - never git's own words."""
    leak = "fatal: bad object deadbeef\nhint: /srv/clones/acme/api/.git"

    def boom(*args: Any, **kwargs: Any):
        raise GitError(leak)

    monkeypatch.setattr(v1_routes, "evidence_from_hunks", boom)
    response = call(
        v1, "POST", "/evidence", json={"target": target_body(), "hunks": hunks_of(v1)}
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "bad_hunk"
    assert "fatal" not in response.text and "/srv/clones" not in response.text


def test_v1_git_budget(
    v1: V1World, generator: type[_Generator], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every heavy route caps the blame calls one request may make."""
    assert GIT_BUDGET == 50
    seen: list[dict] = []

    def spy(repo, target, hunks, **kwargs: Any):  # noqa: ANN001
        seen.append(kwargs)
        return real(repo, target, hunks, **kwargs)

    real = v1_routes.evidence_from_hunks
    monkeypatch.setattr(v1_routes, "evidence_from_hunks", spy)
    assert (
        call(
            v1,
            "POST",
            "/evidence",
            json={"target": target_body(), "hunks": hunks_of(v1)},
        ).status_code
        == 200
    )
    assert (
        call(
            v1,
            "POST",
            "/rationale",
            json={"target": target_body(), "hunks": hunks_of(v1)},
        ).status_code
        == 200
    )
    assert len(seen) == 2
    assert seen and all(kwargs["git_budget"] == GIT_BUDGET for kwargs in seen)


def test_v1_body_too_large(v1: V1World) -> None:
    """A body past the cap is refused before it is parsed."""
    filler = "x" * (MAX_BODY_BYTES + 1)
    v1.client.cookies.clear()
    response = v1.client.post(
        f"{at(ORG)}/api/v1/projects/{SLUG}/evidence",
        content=json.dumps({"target": target_body(), "note": filler}),
        headers={
            "Authorization": f"Bearer {v1.token}",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "body_too_large"


# ---------------------------------------------------------------------------
# Rationale
# ---------------------------------------------------------------------------


def test_rationale_keyed_by_platform_symbol_range(
    v1: V1World, generator: type[_Generator]
) -> None:
    """A known symbol is answered at the platform's own indexed range."""
    response = call(
        v1,
        "POST",
        "/rationale",
        json={
            "target": target_body(start=100, end=120, name="sample.fn"),
            "hunks": [],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    card = RationaleOut.model_validate(body)
    assert (card.target.path, card.target.line_start, card.target.line_end) == (
        "sample.py",
        1,
        3,
    )
    assert card.target.qualified_name == "sample.fn"
    assert card.evidence_count.commits > 0
    assert generator.calls == 1
    assert StatusOut.model_validate(body["project"]).slug == SLUG


def test_rationale_cache_shared_across_working_trees(
    v1: V1World, generator: type[_Generator]
) -> None:
    """Two checkouts' different ranges share one cache row (plan section 0.1 #3)."""
    first = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(start=1, end=3, name="sample.fn"), "hunks": []},
    )
    second = call(
        v1,
        "POST",
        "/rationale",
        json={
            "target": target_body(start=40, end=44, name="sample.fn"),
            "hunks": [],
        },
    )
    assert (first.status_code, second.status_code) == (200, 200), second.text
    assert generator.calls == 1
    assert first.json()["cached_at"] == second.json()["cached_at"]


def test_rationale_without_a_symbol_uses_the_requested_range(
    v1: V1World, generator: type[_Generator]
) -> None:
    """An unindexed name falls back to the hunks over the request's range."""
    response = call(
        v1,
        "POST",
        "/rationale",
        json={
            "target": target_body(name="nosuch.symbol"),
            "hunks": hunks_of(v1),
        },
    )
    assert response.status_code == 200, response.text
    card = RationaleOut.model_validate(response.json())
    assert (card.target.line_start, card.target.line_end) == (1, 3)
    assert generator.calls == 1


def test_rationale_without_evidence_is_404(
    v1: V1World, generator: type[_Generator]
) -> None:
    response = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(path="never/seen.py"), "hunks": []},
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "no_evidence"
    assert generator.calls == 0


def test_rationale_without_a_configured_key_is_409(v1: V1World) -> None:
    """A cache miss with no provider key is refused before any LLM call."""
    response = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(name="sample.fn"), "hunks": []},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "no_llm_key"
    assert v1.state.agent_budget.check(f"card:{v1.org_id}", limit=1) is None


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (lambda: NoEvidenceError("none"), 404, "no_evidence"),
        (lambda: LlmError("the provider said no"), 409, "no_llm_key"),
        (lambda: AnalyzeError("unparseable card"), 409, "no_llm_key"),
        (lambda: GitError("fatal: bad object"), 422, "bad_hunk"),
        (
            lambda: WhyGraphError("bad thing with ghp_0123456789abcdef"),
            422,
            "bad_request",
        ),
    ],
)
def test_v1_error_mapping(
    v1: V1World,
    monkeypatch: pytest.MonkeyPatch,
    raised,  # noqa: ANN001
    status: int,
    code: str,
) -> None:
    """Every refusal the evidence / rationale code can raise has one envelope."""

    def boom(*args: Any, **kwargs: Any):
        raise raised()

    monkeypatch.setattr(v1_routes, "collect_evidence", boom)
    response = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(name="sample.fn"), "hunks": []},
    )
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert "0123456789abcdef" not in response.text


def test_v1_llm_failure_without_a_missing_key_is_503(
    v1: V1World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a key configured, a provider failure is ``503 llm_unavailable``."""

    def boom(*args: Any, **kwargs: Any):
        raise LlmError("rate limited")

    monkeypatch.setattr(v1_routes, "collect_evidence", boom)
    monkeypatch.setattr(v1_routes, "_missing_key", lambda *a, **k: None)
    response = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(name="sample.fn"), "hunks": []},
    )
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "llm_unavailable"


# ---------------------------------------------------------------------------
# The org budgets (plan section 0.1 #7)
# ---------------------------------------------------------------------------


def test_rationale_throttled_per_org(v1: V1World, generator: type[_Generator]) -> None:
    """One generation an hour: the second uncached card is refused, cached ones stay."""
    assert (
        set_defaults(v1, {"rationale": {"agent_generations_per_hour": 1}}).status_code
        == 200
    )

    first = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(name="sample.fn"), "hunks": []},
    )
    assert first.status_code == 200, first.text

    second = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(start=2, end=3), "hunks": hunks_of(v1, 2, 3)},
    )
    assert second.status_code == 429, second.text
    assert second.json()["code"] == "generation_limited"
    assert int(second.headers["Retry-After"]) >= 1
    assert generator.calls == 1

    cached = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(name="sample.fn"), "hunks": []},
    )
    assert cached.status_code == 200, cached.text
    assert generator.calls == 1


def test_rationale_generation_disabled_at_zero(
    v1: V1World, generator: type[_Generator]
) -> None:
    """``0`` serves cached cards only (``403 generation_disabled``)."""
    assert (
        set_defaults(v1, {"rationale": {"agent_generations_per_hour": 0}}).status_code
        == 200
    )
    response = call(
        v1,
        "POST",
        "/rationale",
        json={"target": target_body(name="sample.fn"), "hunks": []},
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "generation_disabled"
    assert generator.calls == 0


class _Descriptor:
    @classmethod
    def from_config(cls, config: object) -> _Descriptor:
        return cls()


@pytest.fixture
def described(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the description backfill; the returned list collects the SHAs."""
    import whygraph.analyze as analyze_pkg

    seen: list[str] = []

    def backfill_all(commits, *, repository, descriptor) -> int:  # noqa: ANN001
        seen.extend(commit.sha for commit in commits)
        return len(seen)

    monkeypatch.setattr(analyze_pkg, "LlmDescriptor", _Descriptor)
    monkeypatch.setattr(analyze_pkg, "backfill_all", backfill_all)
    return seen


def test_v1_backfill_counts_against_org_limit(
    v1: V1World, described: list[str]
) -> None:
    """Each described commit spends one of the org's hourly descriptions (#14)."""
    with use_project(v1.ctx()):
        with get_session() as session:
            undescribed = session.exec(
                select(Commit.sha).where(Commit.llm_description.is_(None))
            ).all()
    assert len(undescribed) >= 2, undescribed

    assert (
        set_defaults(v1, {"analyze": {"agent_descriptions_per_hour": 1}}).status_code
        == 200
    )
    body = {"target": target_body(), "hunks": hunks_of(v1)}
    assert call(v1, "POST", "/evidence", json=body).status_code == 200
    assert len(described) == 1, described
    assert call(v1, "POST", "/evidence", json=body).status_code == 200
    assert len(described) == 1, described


def test_v1_backfill_describes_every_commit_by_default(
    v1: V1World, described: list[str]
) -> None:
    """The default limit is generous enough that nothing is held back."""
    body = {"target": target_body(), "hunks": hunks_of(v1)}
    assert call(v1, "POST", "/evidence", json=body).status_code == 200
    assert len(described) >= 2, described


def test_a_viewer_never_generates_or_backfills(
    v1: V1World,
    generator: type[_Generator],
    described: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A viewer's token: cached cards only, no budget charged (M2f-1 section 4.6)."""
    charged: list[int] = []
    before = v1_routes._before_generate

    def counting(state, org_id: int) -> None:  # noqa: ANN001
        charged.append(org_id)
        before(state, org_id)

    monkeypatch.setattr(v1_routes, "_before_generate", counting)
    with portal_db.get_session() as session:
        session.get(Organization, v1.org_id).default_project_role = "viewer"
    viewer = _mint(v1.ids["cy"], v1.project_id)
    card = {"target": target_body(name="sample.fn"), "hunks": []}

    miss = call(v1, "POST", "/rationale", token=viewer, json=card)
    assert miss.status_code == 403, miss.text
    assert miss.json()["code"] == "generation_not_permitted"
    body = {"target": target_body(), "hunks": hunks_of(v1)}
    assert call(v1, "POST", "/evidence", token=viewer, json=body).status_code == 200
    history = call(v1, "GET", "/history", token=viewer, params={"path": "sample.py"})
    assert history.status_code == 200, history.text
    assert (generator.calls, charged, described) == (0, [], [])

    # The owner generates (and pays for) the card; the viewer is then served it.
    assert call(v1, "POST", "/rationale", json=card).status_code == 200
    assert (generator.calls, charged) == (1, [v1.org_id])
    hit = call(v1, "POST", "/rationale", token=viewer, json=card)
    assert hit.status_code == 200, hit.text
    assert hit.json()["purpose"] == "Holds the sample lines."
    assert (generator.calls, charged) == (1, [v1.org_id])


def test_agent_limit_read_from_org_layer(v1: V1World) -> None:
    """The limits come from the org layer row; a project layer is never consulted."""
    assert org_limit(v1.org_id, "rationale", "agent_generations_per_hour") == 120
    assert org_limit(v1.org_id, "analyze", "agent_descriptions_per_hour") == 600

    with portal_db.get_session() as session:
        save_layer(
            session,
            v1.project_id,
            {"rationale": {"agent_generations_per_hour": 7}},
            org_id=v1.org_id,
        )
    assert org_limit(v1.org_id, "rationale", "agent_generations_per_hour") == 120

    assert (
        set_defaults(v1, {"rationale": {"agent_generations_per_hour": 9}}).status_code
        == 200
    )
    assert org_limit(v1.org_id, "rationale", "agent_generations_per_hour") == 9
    with portal_db.get_session() as session:
        assert (
            load_layer(session, None, org_id=v1.org_id)["rationale"][
                "agent_generations_per_hour"
            ]
            == 9
        )


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("rationale", "agent_generations_per_hour"),
        ("analyze", "agent_descriptions_per_hour"),
    ],
)
def test_agent_limits_not_importable_from_repo(
    tmp_path: Path, section: str, key: str
) -> None:
    """A repo's ``whygraph.toml`` can never raise them (mutation #9)."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "whygraph.toml").write_text(f"[{section}]\n{key} = 9999\n")
    preview = policy.preview_import(root)
    assert key not in preview.layer.get(section, {})
    assert f"{section}.{key}" in {entry["key"] for entry in preview.dropped}
    for name in ("IMPORT_ALLOWLIST", "PUT_ALLOWLIST", "PRODUCTION_PUT_ALLOWLIST"):
        assert key not in getattr(policy, name).get(section, {}), name
    assert policy.DEFAULTS_ALLOWLIST[section][key] is True


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("rationale", "agent_generations_per_hour"),
        ("analyze", "agent_descriptions_per_hour"),
    ],
)
def test_agent_limits_not_settable_per_project(
    v1: V1World, section: str, key: str
) -> None:
    """Production's and local mode's project ``PUT`` both refuse them."""
    v1.client.cookies.clear()
    signed_in(v1.client, v1.ids["ben"])
    response = v1.client.put(
        f"{at(ORG)}/api/projects/{SLUG}/config",
        json={"config": {section: {key: 5}}},
    )
    assert response.status_code == 422, response.text
    assert response.json()["keys"] == [f"{section}.{key}"]
    assert key not in policy.put_allowlist("local").get(section, {})
    assert key not in policy.put_allowlist("production").get(section, {})


@pytest.mark.parametrize("who", ["cy", "fay"])
def test_agent_limits_settable_by_owner_only(v1: V1World, who: str) -> None:
    """``org.configure`` is the owner's alone, so a member or admin is ``403``."""
    refused = set_defaults(
        v1, {"rationale": {"agent_generations_per_hour": 3}}, who=who
    )
    assert refused.status_code == 403, refused.text
    assert (
        set_defaults(v1, {"rationale": {"agent_generations_per_hour": 3}}).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Limits that are not about money
# ---------------------------------------------------------------------------


def test_v1_heavy_concurrency_cap(v1: V1World) -> None:
    """Two heavy requests at once per org; the third is ``503 busy``."""
    in_flight = v1.state.v1_in_flight
    assert in_flight.limit == 2
    assert in_flight.try_acquire(v1.org_id) and in_flight.try_acquire(v1.org_id)
    try:
        response = call(
            v1,
            "POST",
            "/evidence",
            json={"target": target_body(), "hunks": hunks_of(v1)},
        )
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "busy"
        assert response.headers["Retry-After"] == "1"
    finally:
        in_flight.release(v1.org_id)
        in_flight.release(v1.org_id)
    assert (
        call(
            v1,
            "POST",
            "/evidence",
            json={"target": target_body(), "hunks": hunks_of(v1)},
        ).status_code
        == 200
    )


def test_v1_rate_limits_are_per_token(v1: V1World) -> None:
    """The read and heavy throttles are the plan's, keyed by the token."""
    state = v1.state
    assert (state.v1_token.limit, state.v1_token.window) == (600, 60)
    assert (state.v1_heavy.limit, state.v1_heavy.window) == (60, 60)
    for _ in range(state.v1_heavy.limit):
        state.v1_heavy.record(_token_id(v1.token))
    response = call(
        v1, "POST", "/evidence", json={"target": target_body(), "hunks": []}
    )
    assert response.status_code == 429, response.text
    assert response.json()["code"] == "throttled"
    assert int(response.headers["Retry-After"]) >= 1
    assert call(v1, "GET").status_code == 200  # the read throttle is its own


def _token_id(raw: str) -> int:
    with portal_db.get_session() as session:
        return connections.lookup(session, raw).token_id


# ---------------------------------------------------------------------------
# The reads and the resources
# ---------------------------------------------------------------------------


def test_v1_status(v1: V1World) -> None:
    response = call(v1, "GET")
    assert response.status_code == 200, response.text
    status = StatusOut.model_validate(response.json())
    assert (status.slug, status.role, status.access_lost) == (SLUG, "owner", False)
    assert status.github_full_name == f"{ORG}/{SLUG}"


def test_v1_history(v1: V1World) -> None:
    response = call(v1, "GET", "/history", params={"path": "sample.py"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["path"], body["include_renames"]) == ("sample.py", True)
    assert {item["commit"]["sha"] for item in body["evidence"]}
    assert StatusOut.model_validate(body["project"]).slug == SLUG


def test_v1_overview(v1: V1World) -> None:
    response = call(v1, "GET", "/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["commits"] == 2
    assert StatusOut.model_validate(body["project"]).slug == SLUG


def test_v1_commit_resource(v1: V1World) -> None:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=v1.root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    response = call(v1, "GET", f"/commits/{sha}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit"]["sha"] == sha
    assert StatusOut.model_validate(body["project"]).slug == SLUG


@pytest.mark.parametrize("tail", ["/commits/" + "c" * 40, "/prs/4242", "/issues/4242"])
def test_v1_unknown_resource_is_not_found(v1: V1World, tail: str) -> None:
    response = call(v1, "GET", tail)
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def test_v1_and_connect_routes_404_in_local_mode(env: Any) -> None:
    """A local portal serves neither ``/api/v1`` nor ``/api/connect``."""
    with portal_client() as client:
        for path, method in sorted(V1_ROUTES):
            url = (
                path.replace("{slug}", "demo")
                .replace("{sha}", "a" * 40)
                .replace("{number}", "1")
            )
            response = client.request(
                method, url, headers={"Authorization": "Bearer wgc_" + "x" * 43}
            )
            assert response.status_code == 404, (url, method, response.text)
            assert response.json() == {"error": "not found"}
        for path, method in (
            ("/api/connect/validate", "POST"),
            ("/api/connect/token", "POST"),
            ("/api/v1/meta", "GET"),
        ):
            assert client.request(method, path).status_code == 404, path
