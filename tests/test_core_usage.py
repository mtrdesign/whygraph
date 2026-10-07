"""The usage-recording seam, :mod:`whygraph.core.usage` (M2f-2 plan section 4.3).

Unit tests: the unbound no-op, a failing sink, the scope shared across
context copies, the blocked-scope pass-through, and the metered functions
recording each provider call before anything parses it.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from whygraph.analyze import AnalyzeError, LlmDescriptor, RationaleError
from whygraph.analyze.rationale import CommitEvidence
from whygraph.analyze.rationale_generator import RationaleGenerator
from whygraph.core.usage import (
    UsageRecord,
    UsageScope,
    current_usage_sink,
    record_usage,
    set_scope_field,
    usage_blocked,
    use_usage_sink,
)
from whygraph.db.models import Commit
from whygraph.services.llm import (
    CompletionRequest,
    CompletionResponse,
    LlmClient,
    LlmError,
)
from whygraph.services.llm.chat import TurnDone


@dataclass
class CollectingSink:
    """A :class:`~whygraph.core.usage.UsageSink` that keeps what it gets."""

    scope: UsageScope = field(default_factory=lambda: UsageScope(source="explorer"))
    records: list[UsageRecord] = field(default_factory=list)
    blocked: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, rec: UsageRecord) -> None:
        with self.lock:
            self.records.append(rec)

    def blocked_scope(self) -> str | None:
        return self.blocked


class _RaisingSink(CollectingSink):
    def record(self, rec: UsageRecord) -> None:
        raise RuntimeError("ledger down")

    def blocked_scope(self) -> str | None:
        raise RuntimeError("budget map down")


class StubClient(LlmClient):
    """An :class:`LlmClient` returning canned responses, one per call."""

    provider = "anthropic"

    def __init__(self, *texts: str, model: str = "claude-opus-4-7") -> None:
        super().__init__(model=model)
        self._texts = list(texts) or ["a description"]
        self.calls = 0

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "StubClient":
        return cls()

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        text = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        return CompletionResponse(
            text=text,
            model=f"{self.model}-served",
            provider=self.provider,
            input_tokens=100 * self.calls,
            output_tokens=10 * self.calls,
            cache_read_tokens=5,
            reasoning_tokens=2,
        )


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_unbound_is_a_no_op() -> None:
    assert current_usage_sink() is None
    record_usage(
        "chat", TurnDone("stop", 1, 2), provider="openai", model_requested="gpt-4o"
    )
    assert usage_blocked() is None
    set_scope_field(chat_session_id=3)  # nothing to set, nothing raised


def test_record_usage_builds_the_record() -> None:
    sink = CollectingSink()
    done = TurnDone(
        "stop",
        120,
        30,
        cache_read_tokens=20,
        cache_write_tokens=4,
        reasoning_tokens=6,
        cost_usd=0.0123,
        model="gpt-4o-2026-01-01",
    )
    with use_usage_sink(sink):
        record_usage(
            "chat",
            done,
            provider="openrouter",
            model_requested="openai/gpt-4o",
            subject="pkg.mod.fn",
            started=0.0,
        )
    (rec,) = sink.records
    assert rec.task == "chat"
    assert (rec.provider, rec.model_requested, rec.model_served) == (
        "openrouter",
        "openai/gpt-4o",
        "gpt-4o-2026-01-01",
    )
    assert (rec.input_tokens, rec.output_tokens) == (120, 30)
    assert (rec.cache_read_tokens, rec.cache_write_tokens, rec.reasoning_tokens) == (
        20,
        4,
        6,
    )
    assert rec.provider_cost_usd == 0.0123
    assert rec.subject == "pkg.mod.fn"
    assert rec.duration_ms is not None and rec.duration_ms >= 0
    assert current_usage_sink() is None  # the binding ended with the block


def test_a_raising_sink_never_raises_into_the_caller(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger("whygraph"), "propagate", True)
    with use_usage_sink(_RaisingSink()):
        record_usage(
            "analyze",
            CompletionResponse(text="x", model="m", provider="anthropic"),
            provider="anthropic",
            model_requested="m",
        )
        assert usage_blocked() is None
    assert "could not record LLM usage" in caplog.text


def test_usage_blocked_returns_the_sink_scope() -> None:
    sink = CollectingSink(blocked="project")
    with use_usage_sink(sink):
        assert usage_blocked() == "project"


def test_set_scope_field_is_seen_across_context_copies() -> None:
    """The streaming chat body runs each ``next()`` in a fresh context copy."""
    sink = CollectingSink(scope=UsageScope(source="chat"))
    with use_usage_sink(sink):
        contextvars.copy_context().run(set_scope_field, chat_session_id=42)

        def _record() -> None:
            record_usage(
                "chat",
                TurnDone("stop", 1, 1),
                provider="openai",
                model_requested="gpt-4o",
            )

        contextvars.copy_context().run(_record)
    assert sink.scope.chat_session_id == 42
    assert len(sink.records) == 1


def test_set_scope_field_refuses_an_unknown_field() -> None:
    with pytest.raises(TypeError):
        set_scope_field(chat_session=1)


def test_bindings_nest() -> None:
    outer, inner = CollectingSink(), CollectingSink()
    with use_usage_sink(outer):
        with use_usage_sink(inner):
            assert current_usage_sink() is inner
        assert current_usage_sink() is outer


# ---------------------------------------------------------------------------
# The metered functions
# ---------------------------------------------------------------------------


def test_descriptor_records_each_call_with_the_subject() -> None:
    sink = CollectingSink()
    descriptor = LlmDescriptor(StubClient(), max_diff_chars=60)
    diff = "".join(
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n"
        f"-{'x' * 20}\n+{'y' * 20}\n"
        for name in ("a.py", "b.py")
    )
    with use_usage_sink(sink):
        description = descriptor.describe(diff, subject="abc123")
    # Two chunk calls plus a synthesis call - one record per provider call.
    assert [r.subject for r in sink.records] == ["abc123"] * 3
    assert [r.task for r in sink.records] == ["analyze"] * 3
    assert [r.input_tokens for r in sink.records] == [100, 200, 300]
    assert {r.model_served for r in sink.records} == {"claude-opus-4-7-served"}
    assert description.input_tokens == 600  # the description still sums them


def test_descriptor_records_nothing_for_a_failed_call() -> None:
    class _Failing(StubClient):
        def complete(self, request: CompletionRequest) -> CompletionResponse:
            raise LlmError("down")

    sink = CollectingSink()
    with use_usage_sink(sink), pytest.raises(AnalyzeError):
        LlmDescriptor(_Failing()).describe("a diff")
    assert sink.records == []


def _evidence() -> list[CommitEvidence]:
    commit = Commit(
        sha="a1b2c3d4e5f6",
        parent_shas="",
        author_name="A",
        author_email="a@example.com",
        authored_at="2026-01-01T00:00:00+00:00",
        committed_at="2026-01-01T00:00:00+00:00",
        subject="add the thing",
        body="",
        files_changed=1,
        insertions=1,
        deletions=0,
        scanned_at="2026-01-01T00:00:00+00:00",
    )
    return [CommitEvidence(commit)]


def test_rationale_is_recorded_before_it_is_parsed() -> None:
    """A reply that is not valid JSON was still billed (plan section 6.1 #5)."""
    sink = CollectingSink()
    generator = RationaleGenerator(StubClient("this is not json"))
    with use_usage_sink(sink), pytest.raises(RationaleError):
        generator.generate(_evidence(), subject="pkg.mod.fn")
    (rec,) = sink.records
    assert (rec.task, rec.subject, rec.input_tokens) == ("rationale", "pkg.mod.fn", 100)


def test_rationale_success_is_recorded_once() -> None:
    card = {
        "purpose": "p",
        "why": "w",
        "constraints": [],
        "tradeoffs": [],
        "risks": [],
    }
    sink = CollectingSink()
    with use_usage_sink(sink):
        RationaleGenerator(StubClient(json.dumps(card))).generate(_evidence())
    assert len(sink.records) == 1
    assert sink.records[0].subject is None
