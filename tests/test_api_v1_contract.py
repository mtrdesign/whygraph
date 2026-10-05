"""The ``/api/v1`` contract: recorded on the platform, replayed by the client.

Layer 3 of the testing plan (M2e plan section 5.2 item 3). Every ``/api/v1``
route, ``GET /api/v1/meta`` and the ``POST /api/connect/token`` exchange run
against a **real production portal** (the ``v1`` world of
``tests/test_portal_v1.py``) and their ``(status, body)`` is recorded into
``tests/fixtures/api_v1/<route>.json``.

Two tests guard the pair:

* :func:`test_api_v1_contract_fixtures_are_current` records (or re-records
  with ``UPDATE_API_V1_FIXTURES=1``) and fails when a file is stale, so a
  wire change is a visible diff rather than a client that silently drifts.
* :func:`test_platform_client_parses_every_recorded_reply` points
  :class:`~whygraph.portal.platform_client.PlatformHttp` at
  :meth:`~platform_fake.FakePlatform.from_fixtures` and parses every file
  back with the ``api_v1`` models.

The LLM is stubbed on both sides of the recording (``generator`` for the card,
the description backfill in :func:`world`), so ``provider`` / ``model`` name
the stub: the recordings pin the wire **shape**, never a provider's prose.

Bodies are **normalized** before they are written - commit SHAs become
``<sha1>``, ``<sha2>``, ..., timestamps ``<time>`` and a connection token
``<token>`` - so the files are byte-stable across runs. Validation runs on
the raw body, before normalization.
"""

# ruff: noqa: F811 -- pytest fixtures are imported by name

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from pydantic import BaseModel

from platform_fake import ORG as FAKE_ORG
from platform_fake import SLUG as FAKE_SLUG
from platform_fake import TOKEN as FAKE_TOKEN
from platform_fake import FakePlatform
from test_portal_app import at, env, production_env, signed_in  # noqa: F401
from test_portal_connect import consent, query_of
from test_portal_v1 import (  # noqa: F401 -- `v1` and `generator` are fixtures
    ORG,
    SLUG,
    V1World,
    _Generator,
    generator,
    hunks_of,
    target_body,
    v1,
)
from whygraph.api_v1 import (
    EvidenceIn,
    EvidenceReplyOut,
    MetaOut,
    RationaleIn,
    RationaleOut,
    StatusOut,
    TargetIn,
    TokenReply,
)
from whygraph.core.context import use_project
from whygraph.db import get_session
from whygraph.db.models import Issue, PRIssueLink, PullRequest
from whygraph.portal.platform_client import PlatformHttp, PlatformRefused

FIXTURES = Path(__file__).parent / "fixtures" / "api_v1"
UPDATE_ENV = "UPDATE_API_V1_FIXTURES"

PR_NUMBER = 7
ISSUE_NUMBER = 42

_SHA = re.compile(r"\b[0-9a-f]{40}\b")
_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_TOKEN = re.compile(r"wgc_[A-Za-z0-9_-]{20,}")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize(value: Any, shas: dict[str, str]) -> Any:
    """Replace every per-run value with a stable placeholder."""
    if isinstance(value, dict):
        return {key: _normalize(item, shas) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, shas) for item in value]
    if not isinstance(value, str):
        return value
    text = _TOKEN.sub("<token>", value)
    text = _SHA.sub(lambda m: shas.setdefault(m.group(), f"<sha{len(shas) + 1}>"), text)
    return _TIME.sub("<time>", text)


def normalized(status: int, body: Any) -> dict:
    """The recordable form of one answer."""
    return {"status": status, "body": _normalize(body, {})}


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One recorded request.

    Attributes
    ----------
    route : str
        The ``FakePlatform`` route name the recording fills.
    method, path : str
        The request; ``path`` is relative to the host named by ``base``.
    base : {"api", "base"}
        The org host (bearer) or the platform's base host.
    model : type or None
        The ``api_v1`` model the raw body must validate against.
    kwargs : dict
        Extra ``httpx`` request arguments (``json`` / ``params``).
    """

    route: str
    method: str
    path: str
    base: str = "api"
    model: type[BaseModel] | None = None
    kwargs: dict = field(default_factory=dict)


@pytest.fixture
def world(v1: V1World, generator: type[_Generator], monkeypatch) -> V1World:  # noqa: ANN001
    """The ``v1`` world with a merged PR and the issue it closes.

    The description backfill is stubbed out (no provider is configured), so
    every recorded ``llm_description`` is the ``null`` an unanalyzed project
    really has.
    """
    import whygraph.analyze as analyze_pkg

    class _Descriptor:
        @classmethod
        def from_config(cls, config: object) -> _Descriptor:
            return cls()

    monkeypatch.setattr(analyze_pkg, "LlmDescriptor", _Descriptor)
    monkeypatch.setattr(
        analyze_pkg,
        "backfill_all",
        lambda commits, **kwargs: 0,  # noqa: ARG005
    )
    head = head_sha(v1)
    with use_project(v1.ctx()), get_session() as session:
        session.add(
            PullRequest(
                number=PR_NUMBER,
                title="Add the sample module",
                body="Closes #42.",
                state="merged",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-02-01T00:00:00+00:00",
                merged_at="2026-02-01T00:00:00+00:00",
                merge_commit_sha=head,
                head_sha=head,
                base_ref="main",
                author="octocat",
                html_url=f"https://github.com/{ORG}/{SLUG}/pull/{PR_NUMBER}",
                labels='["enhancement"]',
                fetched_at="2026-02-02T00:00:00+00:00",
                commit_titles="[]",
                comments="[]",
            )
        )
        session.add(
            Issue(
                number=ISSUE_NUMBER,
                title="The sample module is missing",
                body="We need a sample.",
                state="closed",
                created_at="2025-12-01T00:00:00+00:00",
                updated_at="2026-02-01T00:00:00+00:00",
                author="reporter",
                html_url=f"https://github.com/{ORG}/{SLUG}/issues/{ISSUE_NUMBER}",
                labels='["bug"]',
                fetched_at="2026-02-02T00:00:00+00:00",
            )
        )
        session.add(
            PRIssueLink(
                pr_number=PR_NUMBER, issue_number=ISSUE_NUMBER, link_kind="closes"
            )
        )
    return v1


def head_sha(w: V1World) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=w.root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def cases(w: V1World) -> list[Case]:
    """Every recorded case, in the order they must run (``revoke`` last)."""
    hunks = hunks_of(w)
    return [
        Case("meta", "GET", "/api/v1/meta", base="base", model=MetaOut),
        Case("token", "POST", "/api/connect/token", base="base", model=TokenReply),
        Case("status", "GET", "", model=StatusOut),
        Case(
            "evidence",
            "POST",
            "/evidence",
            model=EvidenceReplyOut,
            kwargs={"json": {"target": target_body(), "hunks": hunks}},
        ),
        Case(
            "rationale",
            "POST",
            "/rationale",
            model=RationaleOut,
            kwargs={
                "json": {
                    "target": target_body(name="sample.fn"),
                    "hunks": hunks,
                }
            },
        ),
        Case(
            "history",
            "GET",
            "/history",
            kwargs={"params": {"path": "sample.py", "limit": 20}},
        ),
        Case("commit", "GET", f"/commits/{head_sha(w)}"),
        Case("pr", "GET", f"/prs/{PR_NUMBER}"),
        Case("issue", "GET", f"/issues/{ISSUE_NUMBER}"),
        Case("overview", "GET", "/overview"),
        Case("revoke", "DELETE", "/token"),
    ]


def _exchange(w: V1World) -> dict:
    """Walk the consent flow and return the exchange request's JSON body."""
    verifier = "v" * 20 + "-._~" + "A1b2C3d4e5F6g7H8i9J0k" + "z" * 20
    redirect = "http://127.0.0.1:8765/connect/callback"
    w.client.cookies.clear()
    signed_in(w.client, w.ids["ben"])
    response = w.client.post(
        f"{at()}/api/connect/authorize",
        json=consent(
            org=ORG,
            project=SLUG,
            redirect_uri=redirect,
            code_challenge=_challenge(verifier),
        ),
    )
    assert response.status_code == 200, response.text
    code = query_of(response.json()["redirect"])["code"]
    w.client.cookies.clear()
    return {"code": code, "code_verifier": verifier, "redirect_uri": redirect}


def _challenge(verifier: str) -> str:
    from whygraph.portal.github_auth import pkce_challenge

    return pkce_challenge(verifier)


def run_case(w: V1World, case: Case) -> httpx.Response:
    """Send one case's request to the platform."""
    kwargs = dict(case.kwargs)
    if case.route == "token":
        kwargs["json"] = _exchange(w)
    w.client.cookies.clear()
    if case.base == "base":
        return w.client.request(f"{case.method}", at() + case.path, **kwargs)
    return w.client.request(
        case.method,
        f"{at(ORG)}/api/v1/projects/{SLUG}{case.path}",
        headers={"Authorization": f"Bearer {w.token}"},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


def _recorded(route: str) -> dict | None:
    path = FIXTURES / f"{route}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _write(route: str, payload: dict) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{route}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def test_api_v1_contract_fixtures_are_current(world: V1World) -> None:
    """Every case's answer still matches its recorded fixture.

    Set ``UPDATE_API_V1_FIXTURES=1`` to re-record. Each raw body is validated
    with its ``api_v1`` model first, so a recording can never be a shape the
    client would refuse.
    """
    updating = os.environ.get(UPDATE_ENV) == "1"
    stale: list[str] = []
    for case in cases(world):
        response = run_case(world, case)
        body = response.json() if response.content else None
        assert response.status_code == 200 or (
            case.route == "revoke" and response.status_code == 204
        ), f"{case.route}: {response.status_code} {response.text}"
        if case.model is not None:
            case.model.model_validate(body)
        payload = normalized(response.status_code, body)
        if _recorded(case.route) == payload:
            continue
        if updating:
            _write(case.route, payload)
        else:
            stale.append(case.route)
    assert not stale, (
        f"recorded /api/v1 fixtures are stale or missing: {sorted(stale)}; "
        f"re-record with {UPDATE_ENV}=1"
    )


def test_every_recorded_route_has_a_case(world: V1World) -> None:
    """The directory holds exactly the cases - no orphan recording."""
    recorded = {path.stem for path in FIXTURES.glob("*.json")}
    assert recorded == {case.route for case in cases(world)}


# ---------------------------------------------------------------------------
# The replay (layer 2's input)
# ---------------------------------------------------------------------------


def _replayed() -> Iterator[tuple[str, dict]]:
    for path in sorted(FIXTURES.glob("*.json")):
        yield path.stem, json.loads(path.read_text())


def test_platform_client_parses_every_recorded_reply() -> None:
    """``PlatformHttp`` against ``FakePlatform.from_fixtures`` parses it all."""
    fake = FakePlatform.from_fixtures(FIXTURES)
    assert set(fake.responses) == {route for route, _ in _replayed()}

    client = PlatformHttp(
        platform_origin=fake.platform_origin,
        transport=httpx.MockTransport(fake.handle),
    )
    try:
        meta = client.meta()
        assert isinstance(meta, MetaOut) and meta.api_version >= 1
        reply = client.exchange("code", "verifier", "http://127.0.0.1:8765/cb")
        assert isinstance(reply, TokenReply)

        fake.add_token()
        client.bind(FAKE_ORG, FAKE_TOKEN)
        target = TargetIn(path="sample.py", line_start=1, line_end=3)
        assert isinstance(client.status(FAKE_SLUG), StatusOut)
        assert isinstance(
            client.evidence(FAKE_SLUG, EvidenceIn(target=target)), EvidenceReplyOut
        )
        assert isinstance(
            client.rationale(FAKE_SLUG, RationaleIn(target=target)), RationaleOut
        )
        for call in (
            lambda: client.history(FAKE_SLUG, "sample.py"),
            lambda: client.commit(FAKE_SLUG, "a" * 40),
            lambda: client.pr(FAKE_SLUG, PR_NUMBER),
            lambda: client.issue(FAKE_SLUG, ISSUE_NUMBER),
            lambda: client.overview(FAKE_SLUG),
        ):
            assert isinstance(call(), dict)
        client.revoke(FAKE_SLUG)
    finally:
        client.close()


def test_replayed_error_envelopes_refuse(tmp_path: Path) -> None:
    """A recorded refusal replays as a ``PlatformRefused`` with its code."""
    (tmp_path / "status.json").write_text(
        json.dumps({"status": 404, "body": {"error": "gone", "code": "not_found"}})
    )
    fake = FakePlatform.from_fixtures(tmp_path)
    fake.add_token()
    client = PlatformHttp(
        platform_origin=fake.platform_origin,
        transport=httpx.MockTransport(fake.handle),
    )
    client.bind(FAKE_ORG, FAKE_TOKEN)
    try:
        with pytest.raises(PlatformRefused) as caught:
            client.status(FAKE_SLUG)
        assert caught.value.code == "not_found"
    finally:
        client.close()
