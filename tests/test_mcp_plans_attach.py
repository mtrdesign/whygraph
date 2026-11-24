"""``plans`` on the rationale tool and area history (plan 61, section 4.6)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from whygraph.chat import tools as chat_tools
from whygraph.mcp import area_history, rationale
from whygraph.mcp.targets import Target

REF = {"stem": "p", "title": "P", "status": "shipped", "link": "commit", "uri": "u"}


def _target() -> Target:
    return Target(path="a.py", line_start=1, line_end=2, qualified_name="a")


def _evidence(sha: str, source: str = "blame") -> SimpleNamespace:
    return SimpleNamespace(commit=SimpleNamespace(sha=sha), source=source)


@pytest.fixture
def tool_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}
    monkeypatch.setattr(rationale, "resolve_target", lambda **k: _target())
    monkeypatch.setattr(rationale, "current_project", lambda: None)
    monkeypatch.setattr(rationale, "notes_for_target", lambda t: [])
    monkeypatch.setattr(
        rationale,
        "collect_evidence",
        lambda t, limit=20: [_evidence("s1"), _evidence("s2", "area")],
    )

    def fake(shas, paths, **kwargs):
        seen["call"] = (list(shas), list(paths), kwargs)
        return seen.get("plans", [REF])

    monkeypatch.setattr("whygraph.mcp.plans.plans_for", fake)
    seen["fake"] = fake
    return seen


def test_rationale_tool_attaches_symbol_plans(tool_env, monkeypatch) -> None:
    brief = mock.Mock(return_value={"target": {}, "purpose": "x"})
    monkeypatch.setattr(rationale, "_brief", brief)
    card = rationale._rationale_brief_tool(qualified_name="a")
    assert card["plans"] == [REF]
    shas, paths, kwargs = tool_env["call"]
    assert (shas, paths) == (["s1"], ["a.py"])
    assert kwargs["scope"] == "symbol" and kwargs["target"] == _target()
    assert brief.call_args.kwargs["evidence"]  # computed once, passed in


def test_rationale_tool_omits_empty_and_not_cached(tool_env, monkeypatch) -> None:
    monkeypatch.setattr(
        rationale, "_brief", lambda *a, **k: {"status": "not_cached", "target": {}}
    )
    assert "plans" not in rationale._rationale_brief_tool(qualified_name="a")
    monkeypatch.setattr(rationale, "_brief", lambda *a, **k: {"target": {}})
    tool_env["plans"] = []
    assert "plans" not in rationale._rationale_brief_tool(qualified_name="a")


def test_rationale_tool_survives_plans_failure(tool_env, monkeypatch) -> None:
    monkeypatch.setattr(rationale, "_brief", lambda *a, **k: {"target": {}})
    monkeypatch.setattr(
        "whygraph.mcp.plans.plans_for", mock.Mock(side_effect=RuntimeError("boom"))
    )
    assert "plans" not in rationale._rationale_brief_tool(qualified_name="a")


def test_no_evidence_returns_when_only_plans_exist(tool_env, monkeypatch) -> None:
    def raise_none(*a, **k):
        raise rationale.NoEvidenceError("none", code="no_evidence")

    monkeypatch.setattr(rationale, "_brief", raise_none)
    reply = rationale._rationale_brief_tool(qualified_name="a")
    assert reply["status"] == "no_evidence" and reply["plans"] == [REF]
    assert "notes" not in reply
    tool_env["plans"] = []
    with pytest.raises(rationale.NoEvidenceError):
        rationale._rationale_brief_tool(qualified_name="a")


def _area(monkeypatch, plans=None, boom=False) -> list:
    calls: list = []
    item = {
        "commit": {"sha": "c1"},
        "pull_requests": [],
        "issues": [],
        "source": "area",
    }
    monkeypatch.setattr(
        area_history,
        "area_history_payload",
        lambda *a, **k: {
            "path": "a.py",
            "include_renames": True,
            "evidence": [item],
            "describing": 0,
        },
    )
    monkeypatch.setattr(area_history, "shape_evidence", lambda ev, d: {})
    monkeypatch.setattr(area_history, "current_project", lambda: None)

    def fake(*a, **k):
        calls.append((a, k))
        if boom:
            raise RuntimeError("boom")
        return plans or []

    monkeypatch.setattr("whygraph.mcp.plans.plans_for", fake)
    return calls


def test_area_history_attaches_file_plans(monkeypatch) -> None:
    calls = _area(monkeypatch, [REF])
    result = area_history.whygraph_area_history("a.py", detail="full")
    assert result["plans"] == [REF]
    (shas, paths), kwargs = calls[0]
    assert (shas, paths) == (["c1"], ["a.py"])
    assert kwargs == {"scope": "file", "detail": "full"}


def test_area_history_omits_empty_and_survives_failure(monkeypatch) -> None:
    _area(monkeypatch, [])
    assert "plans" not in area_history.whygraph_area_history("a.py")
    _area(monkeypatch, boom=True)
    assert "plans" not in area_history.whygraph_area_history("a.py")


def test_payload_and_chat_carry_no_plans(monkeypatch) -> None:
    _area(monkeypatch, [REF])
    assert "plans" not in area_history.area_history_payload("a.py")
    seen: dict = {}

    def fake_tool(**kwargs):
        seen.update(kwargs)
        return {"evidence": [], "plans": [REF]}

    monkeypatch.setattr(chat_tools, "whygraph_area_history", fake_tool)
    assert chat_tools._get_area_history("a.py") == {"evidence": []}
