"""Tests for the ``whygraph_rationale_brief`` MCP tool.

The evidence collection (blame + DB join) runs for real; only the LLM
round-trip is stubbed — ``RationaleGenerator`` is monkeypatched so no
provider SDK or ``claude`` CLI is touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from whygraph.analyze import AnalyzeError, CommitEvidence, Item, ItemRef, Rationale
from whygraph.db import get_session
from whygraph.db.models import Commit
from whygraph.mcp.errors import WhyGraphError
from whygraph.mcp.rationale import whygraph_rationale_brief
from whygraph.services.codegraph import SymbolContext
from whygraph.services.git import Repository


def _db_commit(
    sha: str,
    *,
    committed_at: str,
    parent_shas: str = "",
    llm_description: str | None = "Mechanical diff summary.",
) -> Commit:
    """A WhyGraph ``commit`` row with sensible defaults for tests."""
    return Commit(
        sha=sha,
        parent_shas=parent_shas,
        author_name="Test User",
        author_email="tester@example.com",
        authored_at=committed_at,
        committed_at=committed_at,
        subject="a change",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-05-01T00:00:00+00:00",
        llm_description=llm_description,
    )


class _FakeGenerator:
    """Stub for :class:`RationaleGenerator` — returns a canned card."""

    @classmethod
    def from_config(cls, config: object) -> "_FakeGenerator":
        return cls()

    def generate(
        self,
        evidence: Sequence[CommitEvidence],
        *,
        symbol_context: SymbolContext | None = None,
        subject: str | None = None,
    ) -> Rationale:
        return Rationale(
            purpose="Holds two sample lines.",
            why="Built up across two commits.",
            constraints=(
                Item(
                    "keep it small",
                    "quoted",
                    (ItemRef("pr", "4", "https://x/pull/4"),),
                    "small",
                ),
            ),
            tradeoffs=(),
            risks=(Item("none worth noting", "inferred"),),
            model="fake-1",
            provider="fake",
        )


def _seed_two_commits(repo_root: Path) -> None:
    """Seed the WhyGraph DB with the two commits of ``temp_git_repo``."""
    newest, oldest = list(Repository(repo_root).commits)
    with get_session() as session:
        session.add(_db_commit(oldest.sha, committed_at="2026-01-01T00:00:00+00:00"))
        session.add(_db_commit(newest.sha, committed_at="2026-02-01T00:00:00+00:00"))


def test_rationale_brief_returns_card(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)

    result = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    assert result["purpose"] == "Holds two sample lines."
    assert result["constraints"] == [
        {
            "text": "keep it small",
            "basis": "quoted",
            "refs": [{"kind": "pr", "id": "4", "url": "https://x/pull/4"}],
            "quote": "small",
        }
    ]
    assert result["tradeoffs"] == []
    assert result["risks"] == [
        {"text": "none worth noting", "basis": "inferred", "refs": []}
    ]
    assert result["model"] == "fake-1"
    assert result["provider"] == "fake"
    assert result["evidence_count"]["commits"] == 2
    assert result["target"]["path"] == "sample.py"


def test_evidence_count_counts_each_pr_and_issue_once() -> None:
    """Two commits of one PR closing one issue: one PR, one issue."""
    from types import SimpleNamespace

    from whygraph.mcp.rationale import _format_response
    from whygraph.mcp.targets import Target

    pr, issue = SimpleNamespace(number=5), SimpleNamespace(number=9)
    other_pr = SimpleNamespace(number=6)
    evidence = [
        SimpleNamespace(pull_requests=(pr,), issues=(issue,)),
        SimpleNamespace(pull_requests=(pr, other_pr), issues=(issue,)),
    ]
    card = _format_response(
        Target(path="sample.py", line_start=1, line_end=3, qualified_name=None),
        _FakeGenerator().generate([]),
        evidence,
        "2026-01-01T00:00:00+00:00",
    )

    assert card["evidence_count"] == {"commits": 2, "prs": 2, "issues": 1}


def test_rationale_brief_errors_without_evidence(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No commits seeded — the blamed SHAs map to nothing in the DB.
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)

    with pytest.raises(WhyGraphError, match="no historical evidence"):
        whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)


def test_rationale_brief_wraps_generator_failure(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)

    class _FailingGenerator:
        @classmethod
        def from_config(cls, config: object) -> "_FailingGenerator":
            return cls()

        def generate(
            self,
            evidence: Sequence[CommitEvidence],
            *,
            symbol_context: SymbolContext | None = None,
            subject: str | None = None,
        ) -> Rationale:
            raise AnalyzeError("model unavailable")

    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FailingGenerator)

    with pytest.raises(WhyGraphError, match="rationale generation failed"):
        whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)


# ---- lazy LLM-description backfill --------------------------------------


def _install_stub_descriptor(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``LlmDescriptor`` with a recording stub; return its diff log."""
    from whygraph.analyze import Description

    seen: list[str] = []

    class _StubDescriptor:
        @classmethod
        def from_config(cls, _cfg: object) -> "_StubDescriptor":
            return cls()

        def describe(self, diff: str, *, subject: str | None = None) -> Description:
            seen.append(diff)
            return Description(
                text="backfilled summary",
                model="stub-1",
                provider="stub",
                input_tokens=1,
                output_tokens=2,
            )

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _StubDescriptor)
    return seen


def _seed_two_commits_with_nulls(repo_root: Path) -> tuple[str, str]:
    """Seed both commits with NULL ``llm_description`` so backfill kicks in.

    Returns ``(newest_sha, oldest_sha)`` for use by assertions.
    """
    newest, oldest = list(Repository(repo_root).commits)
    with get_session() as session:
        session.add(
            _db_commit(
                oldest.sha,
                committed_at="2026-01-01T00:00:00+00:00",
                llm_description=None,
            )
        )
        session.add(
            _db_commit(
                newest.sha,
                committed_at="2026-02-01T00:00:00+00:00",
                parent_shas=oldest.sha,
                llm_description=None,
            )
        )
    return newest.sha, oldest.sha


def test_rationale_brief_backfills_on_cache_miss(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newest_sha, oldest_sha = _seed_two_commits_with_nulls(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    seen = _install_stub_descriptor(monkeypatch)

    result = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    # Rationale still came back — the card is the canned one.
    assert result["purpose"] == "Holds two sample lines."
    # Backfill ran for both commits AND was persisted.
    assert len(seen) == 2
    with get_session() as session:
        for sha in (newest_sha, oldest_sha):
            assert session.get(Commit, sha).llm_description == "backfilled summary"


def test_rationale_brief_skips_backfill_on_cache_hit(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.analyze import Rationale as RationaleVO  # local alias for clarity
    from whygraph.db.models import RationaleCache
    from whygraph.mcp.rationale_cache import _fingerprint

    newest_sha, oldest_sha = _seed_two_commits_with_nulls(temp_git_repo)

    # Build the same evidence shape collect_evidence would, just to derive
    # the fingerprint the cache row needs.
    from whygraph.analyze import CommitEvidence as Ev

    fp = _fingerprint(
        [
            Ev(_db_commit(newest_sha, committed_at="x")),
            Ev(_db_commit(oldest_sha, committed_at="x")),
        ]
    )
    # Seed the cache with a row that matches the target + provider/model the
    # rationale tool will look up. Provider/model come from
    # get_config().cache_identity("rationale") — in tests with no
    # whygraph.toml present, that is ("anthropic", None) -> "default".
    import json

    from whygraph.core import get_config

    provider, pinned_model = get_config().cache_identity("rationale")
    model_key = pinned_model if pinned_model else "default"
    with get_session() as session:
        session.add(
            RationaleCache(
                path="sample.py",
                line_start=1,
                line_end=3,
                provider=provider,
                model=model_key,
                evidence_fingerprint=fp,
                cached_at="2026-03-01T00:00:00+00:00",
                purpose="cached purpose",
                why="cached why",
                constraints=json.dumps(["c1"]),
                tradeoffs=json.dumps([]),
                risks=json.dumps(["r1"]),
                input_tokens=10,
                output_tokens=20,
                actual_provider=provider,
                actual_model="cached-model",
            )
        )

    monkeypatch.chdir(temp_git_repo)
    # If either of these is invoked, the cache-hit short-circuit failed.
    sentinel = {"generator_called": False}

    class _MustNotRun(_FakeGenerator):
        @classmethod
        def from_config(cls, _cfg: object) -> "_MustNotRun":
            sentinel["generator_called"] = True
            return cls()

    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _MustNotRun)
    seen = _install_stub_descriptor(monkeypatch)

    result = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    assert result["purpose"] == "cached purpose"
    # A row of plain strings (before items had sources) serves basis unknown.
    assert result["constraints"] == [{"text": "c1", "basis": "unknown", "refs": []}]
    assert result["risks"] == [{"text": "r1", "basis": "unknown", "refs": []}]
    assert sentinel["generator_called"] is False
    # Crucially, the descriptor was NOT invoked — backfill stayed behind the
    # cache lookup and the rows remain NULL.
    assert seen == []
    with get_session() as session:
        for sha in (newest_sha, oldest_sha):
            assert session.get(Commit, sha).llm_description is None
    # Silence unused-import diagnostics for RationaleVO (kept for type clarity).
    _ = RationaleVO


def test_rationale_brief_backfill_failure_does_not_block_rationale(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.services.llm import LlmError

    from whygraph.mcp.evidence import drain_describes

    newest_sha, oldest_sha = _seed_two_commits_with_nulls(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)

    class _AlwaysFails:
        @classmethod
        def from_config(cls, _cfg: object):
            raise LlmError("no provider configured")

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _AlwaysFails)

    # Rationale still succeeds — backfill is best-effort.
    result = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)
    assert result["purpose"] == "Holds two sample lines."
    # ... but it is never served as final (plan 49 I6).
    assert (result["partial"], result["describing"], result["completing"]) == (
        True,
        2,
        True,
    )
    assert drain_describes(timeout=30)
    # The completion could describe nothing either: the row stays partial.
    assert _row().deferred_shas == " ".join(sorted([newest_sha, oldest_sha]))


# ---- a viewer's context never spends (M2f-1 plan section 4.6) ------------


def _viewer_ctx(root: Path, db_path: Path):  # noqa: ANN202
    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext

    return ProjectContext(
        slug="viewer", root=root, config=Config(whygraph_db=db_path), llm_allowed=False
    )


def _forbid_llm_factories(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every LLM factory record and raise; return the record."""
    built: list[str] = []

    class _Refuse:
        @classmethod
        def from_config(cls, _cfg: object) -> None:
            built.append(cls.__name__)
            raise AssertionError("an LLM client was built")

    class _Descriptor(_Refuse):
        pass

    class _Generator(_Refuse):
        pass

    def make_chat_client(*args: object, **kwargs: object) -> None:
        built.append("chat")
        raise AssertionError("a chat client was built")

    monkeypatch.setattr("whygraph.analyze.LlmDescriptor", _Descriptor)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _Generator)
    monkeypatch.setattr("whygraph.serve.chat.make_chat_client", make_chat_client)
    return built


def test_a_viewer_context_skips_the_description_backfill(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.core.context import use_project
    from whygraph.mcp.evidence import backfill_evidence_descriptions, collect_evidence
    from whygraph.mcp.targets import Target

    newest_sha, oldest_sha = _seed_two_commits_with_nulls(temp_git_repo)
    built = _forbid_llm_factories(monkeypatch)
    target = Target(path="sample.py", line_start=1, line_end=3, qualified_name=None)

    with use_project(_viewer_ctx(temp_git_repo, whygraph_db_initialized)):
        evidence = collect_evidence(target, limit=20)
        assert len(evidence) == 2
        outcome = backfill_evidence_descriptions(evidence, target_path="sample.py")

    assert outcome.waiting == {newest_sha, oldest_sha}
    assert built == []
    with get_session() as session:
        for sha in (newest_sha, oldest_sha):
            assert session.get(Commit, sha).llm_description is None


def test_a_viewer_context_is_served_a_cached_card_and_never_generates(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.core.context import use_project
    from whygraph.mcp.evidence import collect_evidence
    from whygraph.mcp.rationale import GenerationNotPermitted, rationale_card
    from whygraph.mcp.targets import Target

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    built = _forbid_llm_factories(monkeypatch)
    viewer = _viewer_ctx(temp_git_repo, whygraph_db_initialized)
    target = Target(path="sample.py", line_start=1, line_end=3, qualified_name=None)
    charged: list[bool] = []

    with use_project(viewer):
        evidence = collect_evidence(target, limit=20)
        with pytest.raises(GenerationNotPermitted, match="viewer"):
            rationale_card(
                target, evidence, before_generate=lambda: charged.append(True)
            )
    assert (built, charged) == ([], [])  # refused before any budget or client

    # Generated once by someone who may (no context bound: the CLI default) ...
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    rationale_card(target, collect_evidence(target, limit=20))
    built = _forbid_llm_factories(monkeypatch)
    # ... the viewer is then served it from the cache.
    with use_project(viewer):
        card = rationale_card(target, collect_evidence(target, limit=20))
    assert card["purpose"] == "Holds two sample lines."
    assert built == []


class _BlockedSink:
    """A usage sink reporting an exhausted hard-stopped budget (M2f-2)."""

    def __init__(self, scope: str | None = "member", *, after: int = 0) -> None:
        from whygraph.core.usage import UsageScope

        self.scope = UsageScope(source="chat")
        self.blocked = scope
        self.after = after
        self.checks = 0

    def record(self, rec: object) -> None:
        pass

    def blocked_scope(self) -> str | None:
        self.checks += 1
        return self.blocked if self.checks > self.after else None


def test_a_budget_context_refuses_a_card_with_its_scope(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``llm_block="budget_exceeded"`` names the reason and the scope."""
    import dataclasses

    from whygraph.core.context import use_project
    from whygraph.mcp.evidence import collect_evidence
    from whygraph.mcp.rationale import GenerationNotPermitted, rationale_card
    from whygraph.mcp.targets import Target

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    built = _forbid_llm_factories(monkeypatch)
    blocked = dataclasses.replace(
        _viewer_ctx(temp_git_repo, whygraph_db_initialized),
        llm_block="budget_exceeded",
        llm_block_scope="project",
    )
    target = Target(path="sample.py", line_start=1, line_end=3, qualified_name=None)
    with use_project(blocked):
        evidence = collect_evidence(target, limit=20)
        with pytest.raises(GenerationNotPermitted, match="this project") as caught:
            rationale_card(target, evidence)
    assert (caught.value.reason, caught.value.scope) == ("budget_exceeded", "project")
    assert built == []


def test_a_budget_refusal_reaches_the_agent_with_code_and_scope(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over MCP the refusal names its code and scope (§5.3)."""
    import dataclasses

    from mcp.server.fastmcp.exceptions import ToolError

    from whygraph.core.context import use_project
    from whygraph.mcp.errors import log_tool_errors

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    built = _forbid_llm_factories(monkeypatch)
    blocked = dataclasses.replace(
        _viewer_ctx(temp_git_repo, whygraph_db_initialized),
        llm_block="budget_exceeded",
        llm_block_scope="project",
    )
    tool = log_tool_errors(whygraph_rationale_brief)
    with use_project(blocked), pytest.raises(ToolError) as caught:
        tool(path="sample.py", line_start=1, line_end=3)
    text = str(caught.value)
    assert "[WhyGraph] " in text
    assert text.endswith("(code: budget_exceeded, scope: project)")
    assert built == []


def test_a_budget_spent_mid_request_refuses_the_next_card(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat tool's card after earlier rounds spent the budget: the sink knows."""
    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext, use_project
    from whygraph.core.usage import use_usage_sink
    from whygraph.mcp.evidence import collect_evidence
    from whygraph.mcp.rationale import GenerationNotPermitted, rationale_card
    from whygraph.mcp.targets import Target

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    built = _forbid_llm_factories(monkeypatch)
    ctx = ProjectContext(
        slug="p", root=temp_git_repo, config=Config(whygraph_db=whygraph_db_initialized)
    )
    target = Target(path="sample.py", line_start=1, line_end=3, qualified_name=None)
    charged: list[bool] = []
    with use_project(ctx), use_usage_sink(_BlockedSink("org")):
        evidence = collect_evidence(target, limit=20)
        with pytest.raises(GenerationNotPermitted) as caught:
            rationale_card(
                target, evidence, before_generate=lambda: charged.append(True)
            )
    assert (caught.value.reason, caught.value.scope) == ("budget_exceeded", "org")
    assert (built, charged) == ([], [])


def test_force_refresh_skips_the_cache_and_stores_the_new_card(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    calls: list[int] = []

    class _CountingGenerator(_FakeGenerator):
        def generate(self, evidence, *, symbol_context=None, subject=None):
            calls.append(1)
            return super().generate(
                evidence, symbol_context=symbol_context, subject=subject
            )

    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _CountingGenerator)
    args = {"path": "sample.py", "line_start": 1, "line_end": 3}

    whygraph_rationale_brief(**args)
    whygraph_rationale_brief(**args)
    assert len(calls) == 1  # second call served from the cache
    whygraph_rationale_brief(**args, force_refresh=True)
    assert len(calls) == 2  # forced: generated again
    whygraph_rationale_brief(**args)
    assert len(calls) == 2  # and the forced card was stored


def test_force_refresh_still_honours_the_generation_gate(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.analyze import CommitEvidence as _CE
    from whygraph.mcp.rationale import GenerationNotPermitted, rationale_card

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    monkeypatch.setattr("whygraph.mcp.rationale.usage_blocked", lambda: "org")
    from whygraph.mcp.evidence import collect_evidence
    from whygraph.mcp.targets import resolve_target

    target = resolve_target(
        path="sample.py", line_start=1, line_end=3, qualified_name=None
    )
    evidence: list[_CE] = collect_evidence(target, limit=20)
    with pytest.raises(GenerationNotPermitted):
        rationale_card(target, evidence, force_refresh=True)


def test_evidence_for_reports_working_tree_lines_on_blame_items(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp.evidence import whygraph_evidence_for

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)

    result = whygraph_evidence_for(path="sample.py", line_start=1, line_end=3)

    blamed = [e for e in result["evidence"] if e["source"] == "blame"]
    assert blamed
    covered = sorted(n for e in blamed for a, b in e["lines"] for n in range(a, b + 1))
    assert covered == [1, 2, 3]
    assert all(e["lines"] is None for e in result["evidence"] if e["source"] == "area")


# ---- progress and cache notes over a fake MCP session (plan 5.4) -----------


class _FakeSession:
    """Records ``send_progress_notification`` calls the way FastMCP's session takes them."""

    def __init__(self) -> None:
        self.progress: list[tuple] = []

    async def send_progress_notification(
        self,
        progress_token: object,
        progress: float,
        total: float | None = None,
        message: str | None = None,
        related_request_id: str | None = None,
    ) -> None:
        self.progress.append(
            (progress_token, progress, total, message, related_request_id)
        )


def _fake_ctx(token: object | None) -> tuple[object, _FakeSession, list[str]]:
    """A stand-in for FastMCP's ``Context``: request meta, session, ``info``."""
    from types import SimpleNamespace

    session = _FakeSession()
    notes: list[str] = []

    async def info(message: str) -> None:
        notes.append(message)

    meta = SimpleNamespace(progressToken=token)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(meta=meta, session=session),
        request_id="req-7",
        info=info,
    )
    return ctx, session, notes


def _run_on_worker_thread(**kwargs: object) -> dict:
    """Call the tool the way ``offload`` does: on an anyio worker thread."""
    import functools

    import anyio
    import anyio.to_thread

    async def _main() -> dict:
        return await anyio.to_thread.run_sync(
            functools.partial(whygraph_rationale_brief, **kwargs)
        )

    return anyio.run(_main)


def test_rationale_brief_reports_three_progress_steps_on_a_miss(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits_with_nulls(temp_git_repo)
    _install_stub_descriptor(monkeypatch)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    ctx, session, notes = _fake_ctx("tok-1")
    args = {"path": "sample.py", "line_start": 1, "line_end": 3}

    card = _run_on_worker_thread(**args, ctx=ctx)

    assert card["purpose"] == "Holds two sample lines."
    assert session.progress == [
        ("tok-1", 1.0, 3.0, "evidence collected (2 commits)", "req-7"),
        ("tok-1", 2.0, 3.0, "describing 2 commits", "req-7"),
        ("tok-1", 3.0, 3.0, "generating card", "req-7"),
    ]
    assert notes == ["rationale cache miss"]

    # A hit: only the evidence step, and the note says so.
    ctx, session, notes = _fake_ctx("tok-2")
    _run_on_worker_thread(**args, ctx=ctx)
    assert [p[1] for p in session.progress] == [1.0]
    assert notes == ["rationale cache hit"]


def test_rationale_brief_sends_no_progress_without_a_token(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    ctx, session, notes = _fake_ctx(None)

    _run_on_worker_thread(path="sample.py", line_start=1, line_end=3, ctx=ctx)

    assert session.progress == []
    assert notes == ["rationale cache miss"]


def test_a_failing_notification_never_fails_the_card(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    ctx, session, _ = _fake_ctx("tok")

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("stream closed")

    session.send_progress_notification = broken  # type: ignore[method-assign]
    ctx.info = broken  # type: ignore[attr-defined]

    card = _run_on_worker_thread(path="sample.py", line_start=1, line_end=3, ctx=ctx)

    assert card["purpose"] == "Holds two sample lines."


def test_the_context_parameter_is_injected_not_advertised() -> None:
    import asyncio

    from whygraph.mcp.server import mcp

    tool = mcp._tool_manager.get_tool("whygraph_rationale_brief")
    assert tool.context_kwarg == "ctx"
    schemas = {t.name: t.inputSchema for t in asyncio.run(mcp.list_tools())}
    assert "ctx" not in schemas["whygraph_rationale_brief"]["properties"]


def test_card_cites_evidence_and_says_whether_it_was_generated(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    args = {"path": "sample.py", "line_start": 1, "line_end": 3}

    first = whygraph_rationale_brief(**args)
    again = whygraph_rationale_brief(**args)

    assert (first["generated"], again["generated"]) == (True, False)
    assert first["evidence"] == again["evidence"]
    assert 1 <= len(first["evidence"]) <= 5
    ref = first["evidence"][0]
    assert set(ref) == {"sha", "subject", "pull_requests", "issues"}
    assert len(ref["sha"]) == 40 and ref["subject"]
    assert first["evidence_count"]["commits"] >= len(first["evidence"])


def test_cached_only_never_generates(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    calls: list[int] = []

    class _CountingGenerator(_FakeGenerator):
        def generate(self, evidence, *, symbol_context=None, subject=None):
            calls.append(1)
            return super().generate(
                evidence, symbol_context=symbol_context, subject=subject
            )

    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _CountingGenerator)
    args = {"path": "sample.py", "line_start": 1, "line_end": 3}

    miss = whygraph_rationale_brief(**args, cached_only=True)
    assert miss["status"] == "not_cached"
    assert miss["target"]["path"] == "sample.py"
    assert set(miss) == {"status", "target"}
    assert calls == []

    whygraph_rationale_brief(**args)  # pays for the card
    hit = whygraph_rationale_brief(**args, cached_only=True)
    assert hit["generated"] is False and "purpose" in hit
    assert len(calls) == 1


def _add_commit_on_line_two(repo_root: Path) -> None:
    """Rewrite line 2 of ``sample.py`` in a new, scanned commit."""
    import subprocess

    (repo_root / "sample.py").write_text("line one\nline two updated\nline three\n")
    for args in (["add", "sample.py"], ["commit", "-m", "third commit"]):
        subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True)
    newest = list(Repository(repo_root).commits)[0]
    with get_session() as session:
        session.add(_db_commit(newest.sha, committed_at="2026-03-01T00:00:00+00:00"))


def _cache_card(
    line_start: int,
    line_end: int,
    purpose: str,
    *,
    fingerprint: str | None = None,
    qualified_name: str | None = None,
    cached_at: str = "2026-04-01T00:00:00+00:00",
) -> None:
    """Store a card for ``sample.py`` under the configured cache key.

    ``fingerprint`` defaults to the current evidence of the range, so the
    row is fresh.
    """
    import json

    from whygraph.core import get_config
    from whygraph.db.models import RationaleCache
    from whygraph.mcp.evidence import collect_evidence
    from whygraph.mcp.rationale_cache import _fingerprint
    from whygraph.mcp.targets import Target

    if fingerprint is None:
        target = Target("sample.py", line_start, line_end, None)
        fingerprint = _fingerprint(collect_evidence(target, limit=20))
    provider, _model = get_config().cache_identity("rationale")
    with get_session() as session:
        session.add(
            RationaleCache(
                path="sample.py",
                line_start=line_start,
                line_end=line_end,
                provider=provider,
                model="default",
                evidence_fingerprint=fingerprint,
                cached_at=cached_at,
                purpose=purpose,
                why="cached",
                constraints=json.dumps([]),
                tradeoffs=json.dumps([]),
                risks=json.dumps([]),
                actual_model="fake-1",
                qualified_name=qualified_name,
            )
        )


class _NoGenerator(_FakeGenerator):
    """A generator a cached_only call must never reach."""

    def generate(self, evidence, *, symbol_context=None, subject=None):
        raise AssertionError("cached_only must not generate")


def test_cached_only_exact_hit_is_marked_fresh(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _NoGenerator)
    _cache_card(1, 3, "fresh")

    hit = whygraph_rationale_brief(
        path="sample.py", line_start=1, line_end=3, cached_only=True
    )

    assert (hit["purpose"], hit["match"], hit["stale"]) == ("fresh", "exact", False)
    assert hit["generated"] is False and "card_range" not in hit


def test_cached_only_returns_a_stale_exact_card(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    args = {"path": "sample.py", "line_start": 1, "line_end": 3}
    first = whygraph_rationale_brief(**args)
    _add_commit_on_line_two(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _NoGenerator)

    card = whygraph_rationale_brief(**args, cached_only=True)

    assert card["purpose"] == first["purpose"]
    assert card["cached_at"] == first["cached_at"]
    assert (card["match"], card["stale"], card["generated"]) == ("exact", True, False)
    assert card["evidence_count"]["commits"] == 3  # the current evidence
    assert "status" not in card and "card_range" not in card


def test_cached_only_hunk_inside_a_cached_range_gets_its_card(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _NoGenerator)
    _cache_card(1, 3, "the symbol")

    card = whygraph_rationale_brief(
        path="sample.py", line_start=2, line_end=2, cached_only=True
    )

    assert card["purpose"] == "the symbol"
    assert card["match"] == "overlap" and card["stale"] is False
    assert card["card_range"] == {"line_start": 1, "line_end": 3}
    assert card["target"]["line_start"] == card["target"]["line_end"] == 2
    assert card["generated"] is False


def test_cached_only_overlap_card_is_stale_after_a_new_commit(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _NoGenerator)
    _cache_card(1, 3, "before the change")
    _add_commit_on_line_two(temp_git_repo)

    card = whygraph_rationale_brief(
        path="sample.py", line_start=2, line_end=3, cached_only=True
    )

    assert card["purpose"] == "before the change"
    assert (card["match"], card["stale"]) == ("overlap", True)


def test_cached_only_overlap_prefers_the_smallest_containing_range(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlmodel import delete

    from whygraph.db.models import RationaleCache

    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _NoGenerator)
    _cache_card(2, 9, "larger, past the end of the file", fingerprint="old")
    _cache_card(1, 3, "smallest containing")
    _cache_card(3, 3, "overlaps only")
    args = {"path": "sample.py", "line_start": 2, "line_end": 3, "cached_only": True}

    best = whygraph_rationale_brief(**args)
    assert best["purpose"] == "smallest containing"
    assert best["card_range"] == {"line_start": 1, "line_end": 3}

    with get_session() as session:
        session.exec(delete(RationaleCache).where(RationaleCache.line_start == 1))
    # Containing beats a mere overlap, even when the range no longer blames.
    larger = whygraph_rationale_brief(**args)
    assert larger["purpose"] == "larger, past the end of the file"
    assert larger["stale"] is True


def test_cached_only_without_an_overlapping_card_is_not_cached(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _NoGenerator)
    _cache_card(1, 1, "line one only")

    miss = whygraph_rationale_brief(
        path="sample.py", line_start=2, line_end=3, cached_only=True
    )

    assert set(miss) == {"status", "target"} and miss["status"] == "not_cached"


def test_a_generating_call_still_regenerates_a_stale_card(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_commits(temp_git_repo)
    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _FakeGenerator)
    _cache_card(1, 3, "stale", fingerprint="old")
    _cache_card(1, 9, "overlapping", fingerprint="old")

    card = whygraph_rationale_brief(path="sample.py", line_start=1, line_end=3)

    assert card["generated"] is True
    assert card["purpose"] == "Holds two sample lines."
    assert "match" not in card and "stale" not in card


# ---- partial cards and their background completion (plan 49 section 4.3) --


_TARGET_ARGS = {"path": "sample.py", "line_start": 1, "line_end": 3}


def _target():  # noqa: ANN202
    from whygraph.mcp.targets import Target

    return Target(**_TARGET_ARGS, qualified_name=None)


def _row():  # noqa: ANN202
    """The cache row of :func:`_target` under the test config, or ``None``."""
    from whygraph.core import get_config
    from whygraph.mcp.rationale_cache import lookup_exact

    return lookup_exact(_target(), *get_config().cache_identity("rationale"))


class _Describes:
    """What :func:`_fake_describes` records, and how its describe behaves.

    ``described`` lists each ``describe()`` call's SHA. ``unavailable``
    makes the descriptor factory raise ``LlmError``; ``fail`` makes every
    describe raise ``AnalyzeError``; a SHA in ``empty`` has an empty diff
    (terminal). ``gate`` holds each describe until it is set;
    ``on_describe`` runs inside each describe.
    """

    def __init__(self) -> None:
        import threading

        self.lock = threading.Lock()
        self.described: list[str] = []
        self.started = threading.Event()
        self.gate: threading.Event | None = None
        self.unavailable = False
        self.fail = False
        self.empty: set[str] = set()
        self.on_describe = None


def _fake_describes(monkeypatch: pytest.MonkeyPatch) -> _Describes:
    """Stub the descriptor and ``backfill_commit_description`` (no git).

    The fake recipe follows the real one: its own empty-diff check, then
    ``descriptor.describe(...)`` (the backfill's guard), then the DB write
    and the local row.
    """
    import whygraph.analyze as analyze_pkg
    from whygraph.analyze import Description
    from whygraph.services.llm import LlmError

    fake = _Describes()

    class _Descriptor:
        @classmethod
        def from_config(cls, _cfg: object) -> "_Descriptor":
            if fake.unavailable:
                raise LlmError("no provider configured")
            return cls()

        def describe(self, diff: str, *, subject: str | None = None) -> Description:
            with fake.lock:
                fake.described.append(str(subject))
            fake.started.set()
            if fake.gate is not None:
                fake.gate.wait(30)
            if fake.on_describe is not None:
                fake.on_describe(str(subject))
            if fake.fail:
                raise AnalyzeError("provider down")
            return Description(text=f"described {subject}", model="m", provider="fake")

    def backfill_commit(commit, *, repository, descriptor) -> bool:  # noqa: ANN001
        if commit.llm_description is not None or commit.sha in fake.empty:
            return False
        text = descriptor.describe("diff", subject=commit.sha).text
        with get_session() as session:
            row = session.get(Commit, commit.sha)
            if row is not None:
                row.llm_description = text
        commit.llm_description = text
        return True

    monkeypatch.setattr(analyze_pkg, "LlmDescriptor", _Descriptor)
    monkeypatch.setattr(analyze_pkg, "backfill_commit_description", backfill_commit)
    return fake


def _counting_generator(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch the generator with :class:`_FakeGenerator`; return its call log."""
    calls: list[int] = []

    class _Counting(_FakeGenerator):
        def generate(
            self,
            evidence: Sequence[CommitEvidence],
            *,
            symbol_context: SymbolContext | None = None,
            subject: str | None = None,
        ) -> Rationale:
            calls.append(len(evidence))
            return super().generate(evidence)

    monkeypatch.setattr("whygraph.mcp.rationale.RationaleGenerator", _Counting)
    return calls


def _seed_undescribed(count: int) -> list[str]:
    """``count`` undescribed commit rows; their SHAs, newest first."""
    shas = [f"{n + 1:040x}" for n in range(count)]
    with get_session() as session:
        for n, sha in enumerate(shas):
            session.add(
                _db_commit(
                    sha,
                    committed_at=f"2026-01-{28 - n:02d}T00:00:00+00:00",
                    llm_description=None,
                )
            )
    return shas


def _evidence(shas: list[str]) -> list[CommitEvidence]:
    """Fresh detached evidence over the rows of ``shas``, in that order."""
    with get_session() as session:
        rows = [session.get(Commit, sha) for sha in shas]
        session.expunge_all()
    return [CommitEvidence(commit=row) for row in rows]


def _card(shas: list[str], **kwargs: object) -> dict:
    from whygraph.mcp.rationale import rationale_card

    return rationale_card(_target(), _evidence(shas), mark_generated=True, **kwargs)


def _partial_card(
    monkeypatch: pytest.MonkeyPatch, fake: _Describes, shas: list[str]
) -> None:
    """Store a partial card for ``shas`` (no descriptor), then let describes work."""
    from whygraph.mcp.evidence import drain_describes

    fake.unavailable = True
    card = _card(shas)
    assert (card["partial"], card["describing"]) == (True, len(shas))
    assert drain_describes(timeout=30)  # its completion could do nothing either
    fake.unavailable = False
    monkeypatch.setattr("whygraph.mcp.rationale.CARD_RETRY_COOLDOWN_SEC", 0)


def test_a_cold_card_describes_every_commit_inside_the_wall(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 1: twelve fast describes, all done before the card."""
    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    shas = _seed_undescribed(12)

    card = _card(shas)

    assert (card["partial"], card["describing"], card["completing"]) == (
        False,
        0,
        False,
    )
    assert card["generated"] is True
    assert sorted(fake.described) == sorted(shas)
    assert calls == [12]
    assert _row().deferred_shas == ""
    with get_session() as session:
        assert all(session.get(Commit, sha).llm_description for sha in shas)


def test_a_partial_card_is_completed_in_the_background_once(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 2: stored partial, completed later, then served final."""
    from whygraph.mcp.evidence import drain_describes

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    shas = _seed_undescribed(3)

    _partial_card(monkeypatch, fake, shas)
    assert _row().deferred_shas == " ".join(sorted(shas))
    assert calls == [3]

    hit = _card(shas)
    assert (hit["partial"], hit["describing"], hit["completing"]) == (True, 3, True)
    assert hit["generated"] is False
    assert drain_describes(timeout=30)

    assert sorted(fake.described) == sorted(shas)
    assert len(calls) == 2  # regenerated once
    final = _card(shas)
    assert (final["partial"], final["describing"], final["completing"]) == (
        False,
        0,
        False,
    )
    assert final["generated"] is False
    assert len(calls) == 2


def test_a_partial_hit_returns_at_once_and_a_second_hit_starts_nothing(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 3: a hit never waits for a describe, and is single-flight."""
    import threading
    import time

    from whygraph.mcp import rationale
    from whygraph.mcp.evidence import drain_describes

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    shas = _seed_undescribed(2)
    _partial_card(monkeypatch, fake, shas)
    fake.gate = threading.Event()

    try:
        started = time.monotonic()
        first = _card(shas)
        assert time.monotonic() - started < 1.0
        assert fake.started.wait(5)  # the completion is describing
        second = _card(shas)
        assert first["completing"] is True and second["completing"] is True
        assert first["partial"] is True and second["partial"] is True
        assert len(rationale._completions_running) == 1
    finally:
        fake.gate.set()
    assert drain_describes(timeout=30)

    assert sorted(fake.described) == sorted(shas)  # each described once
    assert len(calls) == 2
    assert _row().deferred_shas == ""


@pytest.mark.parametrize("why", ["terminal", "failing"])
def test_a_completion_that_gains_no_description_never_regenerates(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
    why: str,
) -> None:
    from whygraph.mcp.evidence import drain_describes

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    shas = _seed_undescribed(2)
    _partial_card(monkeypatch, fake, shas)
    if why == "terminal":
        fake.empty = set(shas)
    else:
        fake.fail = True

    assert _card(shas)["completing"] is True
    assert drain_describes(timeout=30)

    assert calls == [2]  # only the first card
    # Terminal commits make the card final; failing ones keep it partial.
    expected = "" if why == "terminal" else " ".join(sorted(shas))
    assert _row().deferred_shas == expected


def test_ten_hits_on_a_failing_describe_attempt_it_at_most_twice(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 3: the cooldown stops a card re-spending on every hit."""
    from whygraph.mcp.evidence import drain_describes

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    shas = _seed_undescribed(1)
    fake.fail = True

    miss = _card(shas)  # one inline attempt, then the completion's
    assert (miss["partial"], miss["completing"]) == (True, True)
    assert drain_describes(timeout=30)
    hits = [_card(shas) for _ in range(10)]
    assert drain_describes(timeout=30)

    assert all(hit["partial"] and not hit["completing"] for hit in hits)
    assert len(fake.described) == 2
    assert calls == [1]


def test_a_miss_stores_the_commits_allow_refused(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    _counting_generator(monkeypatch)
    # No completion: the stored row is the miss's own.
    monkeypatch.setattr("whygraph.mcp.rationale.COMPLETIONS_MAX", 0)
    shas = _seed_undescribed(12)
    lock = threading.Lock()
    refused: list[bool] = []

    def allow() -> bool:
        with lock:
            if len(refused) < 3:
                refused.append(True)
                return False
            return True

    card = _card(shas, allow_description=allow)

    waiting = sorted(set(shas) - set(fake.described))
    assert len(waiting) == 3
    assert (card["partial"], card["describing"], card["completing"]) == (
        True,
        3,
        False,
    )
    assert _row().deferred_shas == " ".join(waiting)


def test_a_slow_describe_returns_a_partial_card_at_the_wall(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 4, scaled: a 1 s wall instead of 20 s, a describe that hangs."""
    import threading
    import time

    from whygraph.mcp import evidence as ev

    monkeypatch.chdir(temp_git_repo)
    monkeypatch.setitem(
        ev.backfill_evidence_descriptions.__kwdefaults__, "wall_sec", 1.0
    )
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    shas = _seed_undescribed(2)
    fake.gate = threading.Event()

    try:
        started = time.monotonic()
        card = _card(shas)
        elapsed = time.monotonic() - started
    finally:
        fake.gate.set()
    assert elapsed < 4.0
    assert (card["partial"], card["describing"], card["completing"]) == (
        True,
        2,
        True,
    )
    assert ev.drain_describes(timeout=30)
    # The late describes landed; the completion found them and regenerated.
    assert _row().deferred_shas == ""
    assert len(calls) == 2
    assert sorted(fake.described) == sorted(shas)


def test_a_cached_only_hit_on_a_partial_card_starts_nothing(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp import rationale

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    _counting_generator(monkeypatch)
    shas = _seed_undescribed(2)
    _partial_card(monkeypatch, fake, shas)
    fake.described.clear()

    card = _card(shas, cached_only=True)

    assert (card["partial"], card["describing"], card["completing"]) == (
        True,
        2,
        False,
    )
    assert card["match"] == "exact"
    assert rationale._completions_running == set()
    assert fake.described == []


def test_a_completion_stops_when_the_row_moved_before_generating(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.core import get_config
    from whygraph.mcp.evidence import drain_describes
    from whygraph.mcp.rationale_cache import store_cached

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    shas = _seed_undescribed(2)
    _partial_card(monkeypatch, fake, shas)
    newer = Rationale(
        purpose="refreshed meanwhile",
        why="w",
        constraints=(),
        tradeoffs=(),
        risks=(),
        model="m",
        provider="fake",
    )

    def refresh(_sha: str) -> None:
        # A forced refresh lands while the completion describes.
        store_cached(
            _target(),
            _evidence(shas),
            newer,
            *get_config().cache_identity("rationale"),
        )

    fake.on_describe = refresh
    charged: list[bool] = []

    assert _card(shas, before_generate=lambda: charged.append(True))["completing"]
    assert drain_describes(timeout=30)

    assert charged == []
    assert calls == [2]
    row = _row()
    assert (row.rationale.purpose, row.deferred_shas) == ("refreshed meanwhile", "")


def test_a_before_generate_refusal_leaves_the_card_partial(
    temp_git_repo: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whygraph.mcp.evidence import drain_describes

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    shas = _seed_undescribed(2)
    _partial_card(monkeypatch, fake, shas)
    charged: list[bool] = []

    def refuse() -> None:
        charged.append(True)
        raise RuntimeError("hourly card limit reached")

    assert _card(shas, before_generate=refuse)["completing"] is True
    assert drain_describes(timeout=30)

    assert charged == [True]
    assert calls == [2]
    assert sorted(fake.described) == sorted(shas)
    assert _row().deferred_shas == " ".join(sorted(shas))


def test_completions_are_per_project_db(
    temp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 6: same path and range, own completion and cooldown per project."""
    import threading

    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext, current_project, use_project
    from whygraph.mcp import rationale
    from whygraph.mcp.evidence import drain_describes

    gate = threading.Event()
    ran: list[str] = []

    def complete(*args: object, **kwargs: object) -> None:
        ran.append(current_project().slug)
        gate.wait(10)

    monkeypatch.setattr(rationale, "_complete_partial", complete)

    def start(slug: str) -> bool:
        ctx = ProjectContext(
            slug=slug,
            root=temp_git_repo,
            config=Config(whygraph_db=tmp_path / slug / "whygraph.db"),
        )
        with use_project(ctx):
            return rationale._start_completion(_target(), [], "fake", None, None, None)

    try:
        assert start("a") is True
        assert start("b") is True  # not "already running": its own key
        assert len(rationale._completions_running) == 2
        assert start("c") is False  # COMPLETIONS_MAX are running
        assert start("a") is True  # already running for a
    finally:
        gate.set()
    assert drain_describes(timeout=30)

    assert sorted(ran) == ["a", "b"]
    assert start("a") is False and start("b") is False  # each in its cooldown
    assert start("c") is True
    assert drain_describes(timeout=30)


def test_stopping_a_project_mid_completion_recreates_nothing(
    temp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 6: a removed project's completion writes nothing after the stop."""
    import shutil
    import threading

    from whygraph.core.config import Config
    from whygraph.core.context import ProjectContext, use_project
    from whygraph.db.bootstrap import ensure_initialized
    from whygraph.db.engine import dispose_engine
    from whygraph.mcp.evidence import drain_describes, stop_describes

    monkeypatch.chdir(temp_git_repo)
    fake = _fake_describes(monkeypatch)
    calls = _counting_generator(monkeypatch)
    history = tmp_path / "history"
    db_path = history / "whygraph.db"
    ctx = ProjectContext(
        slug="gone", root=temp_git_repo, config=Config(whygraph_db=db_path)
    )
    with use_project(ctx):
        ensure_initialized()
        shas = _seed_undescribed(2)
        _partial_card(monkeypatch, fake, shas)
        fake.gate = threading.Event()
        assert _card(shas)["completing"] is True
    try:
        assert fake.started.wait(5)  # the completion is describing
        stop_describes(db_path)
        dispose_engine(db_path)
        shutil.rmtree(history)
    finally:
        fake.gate.set()
    assert drain_describes(timeout=30)

    assert not history.exists()
    assert calls == [2]
