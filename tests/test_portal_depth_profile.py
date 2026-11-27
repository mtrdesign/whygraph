"""The depth estimate (plan 62 section 14.5a, R33): ``GET .../rationale/depth-profile``.

The route inventory (``ROUTE_ACTIONS``) and the linked project's refusal
(``REFUSED``) live beside the other route tables; this file covers the
computation: ``computing`` first, then the priced sample, no model call at
any point, the no-index state, a failed sample answering once, and the
sampler itself over a fake CodeGraph index.
"""

# ruff: noqa: F811 -- pytest fixtures (`env`, ...) are imported

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from conftest import build_fake_codegraph_db
from test_portal_app import env  # noqa: F401 -- fixture
from test_portal_serve_api import _EDGES, _NODES, portal_with_project
from whygraph.analyze import CommitEvidence
from whygraph.analyze.rationale_generator import RationaleGenerator
from whygraph.core.config import Config
from whygraph.db.models import Commit
from whygraph.portal import depth_profile
from whygraph.portal.prices import NO_OVERRIDES
from whygraph.services.llm import LlmClientFactory

LEVEL_ROW = {"input_tokens": 12_000, "undescribed": 2, "commits": 6}


def _raw(n: int = 3) -> dict:
    """A measured sample with ``n`` identical targets."""
    target = {
        "low": dict(LEVEL_ROW, input_tokens=10_000),
        "medium": dict(LEVEL_ROW, input_tokens=12_000),
        "high": dict(LEVEL_ROW, input_tokens=14_000, verify_input_tokens=2_000),
    }
    return {
        "status": "ready",
        "targets": [target] * n,
        "sampled": n,
        "computed_at": "2026-11-27T10:00:00+00:00",
    }


@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fail the test when anything makes a client or completes a request."""
    calls = SimpleNamespace(n=0)

    def boom(*args, **kwargs):
        calls.n += 1
        raise AssertionError("the depth estimate must not call a model")

    monkeypatch.setattr(LlmClientFactory, "make", boom)
    monkeypatch.setattr(RationaleGenerator, "from_config", classmethod(boom))
    return calls


def _wait_ready(client, path: str = "/api/rationale/depth-profile") -> dict:
    for _ in range(100):
        body = client.get(path).json()
        if body.get("status") != "computing":
            return body
        time.sleep(0.05)
    raise AssertionError("the estimate never finished")


def test_computing_then_ready_without_a_model_call(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, no_llm: SimpleNamespace
) -> None:
    gate = threading.Event()
    started = []

    def fake_sample(root, config, **kwargs):
        started.append(1)
        gate.wait(5)
        return _raw()

    monkeypatch.setattr(depth_profile, "sample_profile", fake_sample)
    with portal_with_project(env, nodes=_NODES, edges=_EDGES) as client:
        assert client.get("/api/rationale/depth-profile").json() == {
            "status": "computing"
        }
        # A second ask while it runs joins the one computation.
        assert client.get("/api/rationale/depth-profile").json() == {
            "status": "computing"
        }
        gate.set()
        body = _wait_ready(client)
        assert started == [1]
        assert body["status"] == "ready" and body["sampled"] == 3
        assert body["cost_hidden"] is False
        levels = body["levels"]
        assert levels["low"]["calls"] == 1 and levels["high"]["calls"] == 2
        assert levels["low"]["input_tokens"] == {"p50": 10_000, "p90": 10_000}
        # High reads the target's source once more for its verification pass.
        assert levels["high"]["input_tokens"]["p50"] == 16_000
        assert levels["high"]["output_tokens"] == (
            depth_profile.DEFAULT_DRAFT_OUTPUT_TOKENS
            + depth_profile.VERIFY_OUTPUT_TOKENS
        )
        assert body["output_source"] == "default"
        assert body["descriptions"]["undescribed_share"] == pytest.approx(
            1 / 3, abs=0.001
        )
        # Cached per project and HEAD: no second computation.
        assert client.get("/api/rationale/depth-profile").json()["status"] == "ready"
        assert started == [1]
    assert no_llm.n == 0


def test_price_follows_the_pinned_model_and_is_unknown_without_one(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, no_llm: SimpleNamespace
) -> None:
    monkeypatch.setattr(depth_profile, "sample_profile", lambda *a, **k: _raw())
    with portal_with_project(env, nodes=_NODES, edges=_EDGES) as client:
        body = _wait_ready(client)
        # No pinned model and no rationale usage yet: the price is unknown.
        assert body["model"]["price_known"] is False
        assert body["levels"]["medium"]["cost"] is None
        assert body["prices_as_of"] is None


def test_a_priced_model_gives_a_range_and_a_first_card_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config.from_dict(
        {"llm": {"model": "anthropic/claude-sonnet-4-5"}},
        __import__("pathlib").Path("."),
    )
    monkeypatch.setattr(depth_profile, "_ledger", lambda *a, **k: [])
    raw = _raw()
    raw["targets"][0] = {
        **raw["targets"][0],
        "medium": dict(LEVEL_ROW, input_tokens=30_000),
    }
    body = depth_profile.render_profile(
        raw, config, project_id=1, overrides=NO_OVERRIDES, show_cost=True, head="h"
    )
    assert body["model"]["source"] == "pinned" and body["model"]["price_known"]
    cost = body["levels"]["medium"]["cost"]
    assert 0 < cost["low"] <= cost["high"]
    first = body["levels"]["medium"]["first_card"]
    assert first["low"] > cost["low"]  # two undescribed commits add their price
    assert body["descriptions"]["per_commit_usd"] > 0


def test_a_caller_without_usage_sees_no_tokens_or_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(depth_profile, "_ledger", lambda *a, **k: [])
    body = depth_profile.render_profile(
        _raw(),
        Config.from_dict({}, __import__("pathlib").Path(".")),
        project_id=1,
        overrides=NO_OVERRIDES,
        show_cost=False,
        head="h",
    )
    assert body["cost_hidden"] is True
    assert body["levels"]["low"]["input_tokens"] is None
    assert body["levels"]["low"]["cost"] is None
    assert body["descriptions"]["per_commit_usd"] is None


def test_no_index_is_a_state_not_an_error(
    env: SimpleNamespace, no_llm: SimpleNamespace
) -> None:
    with portal_with_project(env, codegraph=False) as client:
        assert _wait_ready(client) == {"status": "no_index"}


def test_no_history_is_a_state_too(
    env: SimpleNamespace, no_llm: SimpleNamespace
) -> None:
    """An index whose files have no scanned history answers `no_history`, not an error."""
    with portal_with_project(env, nodes=_NODES, edges=_EDGES) as client:
        assert _wait_ready(client) == {"status": "no_history"}


def test_a_failed_sample_answers_once_then_computes_again(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, no_llm: SimpleNamespace
) -> None:
    runs = []

    def flaky(root, config, **kwargs):
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("boom")
        return _raw()

    monkeypatch.setattr(depth_profile, "sample_profile", flaky)
    with portal_with_project(env, nodes=_NODES, edges=_EDGES) as client:
        assert (
            client.get("/api/rationale/depth-profile").json()["status"] == "computing"
        )
        for _ in range(100):
            response = client.get("/api/rationale/depth-profile")
            if response.status_code != 200:
                break
            time.sleep(0.05)
        assert response.status_code == 500
        assert response.json()["code"] == "depth_profile_failed"
        # The failure was handed out once: Retry computes again.
        assert (
            client.get("/api/rationale/depth-profile").json()["status"] == "computing"
        )
        assert _wait_ready(client)["status"] == "ready"


def test_sample_profile_measures_each_level_over_the_most_referenced_files(
    tmp_path, monkeypatch: pytest.MonkeyPatch, no_llm: SimpleNamespace
) -> None:
    """Targets are the largest symbol of the top files; every level gets a size."""
    root = tmp_path / "repo"
    (root / ".codegraph").mkdir(parents=True)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "a.py").write_text(
        "\n".join(f"x{i} = {i}" for i in range(40))
    )
    build_fake_codegraph_db(
        root / ".codegraph" / "codegraph.db", nodes=_NODES, edges=_EDGES
    )
    config = Config.from_dict({}, root)

    commit = Commit(
        sha="a" * 40,
        parent_shas="[]",
        author_name="Jane",
        author_email="j@example.com",
        authored_at="2026-05-01T12:00:00Z",
        committed_at="2026-05-01T12:00:00Z",
        subject="add m",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-05-02T00:00:00Z",
        llm_description=None,
    )
    seen = []

    class FakeRepo:
        def __init__(self, path):
            self.root = path

        def blame(self, path, start, end):
            seen.append((path, start, end))
            return []

    monkeypatch.setattr(depth_profile, "Repository", FakeRepo)
    monkeypatch.setattr(
        depth_profile,
        "evidence_from_hunks",
        lambda *a, **k: SimpleNamespace(
            evidence=[CommitEvidence(commit=commit, source="blame")]
        ),
    )
    monkeypatch.setattr(
        depth_profile,
        "review_scope",
        lambda *a, **k: SimpleNamespace(
            paths=frozenset({"src/pkg/a.py"}), current_lines=("x",) * 40
        ),
    )

    @__import__("contextlib").contextmanager
    def session():
        yield None

    monkeypatch.setattr(depth_profile, "project_session", session)
    raw = depth_profile.sample_profile(
        root, config, git_budget=5, median_description_chars=400
    )
    assert raw["status"] == "ready" and raw["sampled"] == 1
    # The largest definable symbol of the referenced file is the class A (3-30).
    assert seen == [("src/pkg/a.py", 3, 30)]
    (target,) = raw["targets"]
    assert set(target) == {"low", "medium", "high"}
    assert target["low"]["undescribed"] == 1 and target["low"]["input_tokens"] > 0
    assert "verify_input_tokens" in target["high"]
    assert "verify_input_tokens" not in target["low"]
    assert no_llm.n == 0
