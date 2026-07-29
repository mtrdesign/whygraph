"""Tests for the chat tool registry and file tools (plan §5.1, §10, §14).

Three concerns, in order of how much they'd cost to get wrong:

1. **The file clamp** (§10) — the one genuinely new attack surface. Every
   acceptance-criterion case from AC #6 is here, plus binary sniffing and
   line-range bounds.
2. **The rationale generation budget** (§0.1) — a cache miss must call
   ``whygraph_rationale_brief`` *verbatim* (zero drift from the MCP tool),
   exactly once, and stop calling it once the per-turn budget is spent.
3. **Dispatch discipline** — unknown names, bad arguments, and backend
   failures come back as tool *results*, never exceptions, so a bad call
   can't end the user's turn.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from whygraph import core
from whygraph.chat import tools as chat_tools
from whygraph.chat.tools import (
    MAX_RESULT_CHARS,
    TOOL_SPECS,
    TRUNCATION_MARKER,
    ToolRegistry,
)
from whygraph.core.config import ChatConfig, Config
from whygraph.db import engine as db_engine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NODES = [
    {
        "id": "n_file_a",
        "kind": "file",
        "name": "a.py",
        "qualified_name": "src/pkg/a.py",
        "file_path": "src/pkg/a.py",
        "language": "python",
        "start_line": 1,
        "end_line": 40,
        "docstring": None,
        "signature": None,
    },
    {
        "id": "n_run_turn",
        "kind": "function",
        "name": "run_turn",
        "qualified_name": "pkg.a.run_turn",
        "file_path": "src/pkg/a.py",
        "language": "python",
        "start_line": 3,
        "end_line": 12,
        "docstring": "runs a turn",
        "signature": "def run_turn()",
    },
    {
        "id": "n_caller",
        "kind": "function",
        "name": "caller",
        "qualified_name": "pkg.b.caller",
        "file_path": "src/pkg/b.py",
        "language": "python",
        "start_line": 2,
        "end_line": 8,
        "docstring": None,
        "signature": "def caller()",
    },
]
_EDGES = [
    ("n_file_a", "n_run_turn", "contains"),
    ("n_caller", "n_run_turn", "calls"),
]


@pytest.fixture
def chat_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codegraph_db_factory):
    """A fake repo root with a CodeGraph index and an empty WhyGraph DB.

    ``repo_root()`` walks up from cwd to a ``.git`` marker, so the fixture
    creates one and chdirs in — that is what makes the file clamp's notion
    of "inside the repo" testable.
    """
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "a.py").write_text(
        "".join(f"line {n}\n" for n in range(1, 31)), encoding="utf-8"
    )
    (root / "whygraph.toml").write_text('log_level = "INFO"\n', encoding="utf-8")
    (root / ".env").write_text("SECRET=abc\n", encoding="utf-8")
    (root / ".whygraph").mkdir()
    (root / ".whygraph" / "whygraph.db").write_bytes(b"\x00fake sqlite")
    (root / "README.md").write_text("# hi\n", encoding="utf-8")

    cg_path = codegraph_db_factory(nodes=_NODES, edges=_EDGES)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        core,
        "_config",
        Config(whygraph_db=root / ".whygraph" / "wg.db", codegraph_db=cg_path),
    )
    db_engine._reset_engine()
    try:
        yield root
    finally:
        db_engine._reset_engine()
        core._reset_config()


def _result(registry: ToolRegistry, name: str, **arguments) -> dict:
    """Dispatch and JSON-decode, so tests assert on structure not strings."""
    return json.loads(registry.dispatch(name, arguments))


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def test_eleven_tools_with_unique_names_and_object_schemas() -> None:
    names = [spec.name for spec in TOOL_SPECS]
    assert len(names) == 11
    assert len(set(names)) == 11
    assert set(names) == {
        "search_symbols",
        "get_symbol",
        "get_rationale",
        "get_evidence",
        "get_area_history",
        "get_commit",
        "get_pr",
        "get_issue",
        "get_repo_overview",
        "read_file",
        "list_dir",
    }
    for spec in TOOL_SPECS:
        assert spec.parameters["type"] == "object"
        assert spec.description  # the model's only guidance


def test_every_spec_has_a_handler() -> None:
    """No spec may be advertised without something to dispatch to."""
    registry = ToolRegistry(max_rationale_generations=0)
    for spec in TOOL_SPECS:
        assert spec.name in registry._handlers


def test_registry_specs_are_the_module_constant(chat_repo: Path) -> None:
    assert ToolRegistry(max_rationale_generations=0).specs is TOOL_SPECS


# ---------------------------------------------------------------------------
# CodeGraph tools
# ---------------------------------------------------------------------------


def test_search_symbols_finds_by_substring(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "search_symbols", query="run_turn")
    assert result["count"] == 1
    assert result["symbols"][0]["qualified_name"] == "pkg.a.run_turn"


def test_search_symbols_requires_a_query(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert "error" in _result(registry, "search_symbols", query="")


def test_get_symbol_returns_identity_and_relations(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_symbol", qualified_name="pkg.a.run_turn")
    assert result["symbol"]["name"] == "run_turn"
    assert [c["qualified_name"] for c in result["relations"]["callers"]] == [
        "pkg.b.caller"
    ]
    assert result["relations"]["container"]["qualified_name"] == "src/pkg/a.py"


def test_get_symbol_works_on_file_nodes(chat_repo: Path) -> None:
    """A file's qualified name yields its outline — the file-outline affordance."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_symbol", qualified_name="src/pkg/a.py")
    assert result["symbol"]["kind"] == "file"
    assert [c["qualified_name"] for c in result["relations"]["children"]] == [
        "pkg.a.run_turn"
    ]


def test_get_symbol_unknown_name_is_an_error_result(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert (
        "not found"
        in _result(registry, "get_symbol", qualified_name="nope.zzz")["error"]
    )


def test_missing_codegraph_degrades_to_an_error_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No index → an error *result*, so the WhyGraph tools keep working."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        core,
        "_config",
        Config(whygraph_db=root / "wg.db", codegraph_db=root / "nope" / "cg.db"),
    )
    db_engine._reset_engine()
    try:
        registry = ToolRegistry(max_rationale_generations=0)
        result = _result(registry, "search_symbols", query="x")
        assert "CodeGraph index unavailable" in result["error"]
    finally:
        db_engine._reset_engine()
        core._reset_config()


# ---------------------------------------------------------------------------
# Dispatch discipline
# ---------------------------------------------------------------------------


def test_unknown_tool_name_is_rejected(chat_repo: Path) -> None:
    """The registry is a closed allow-list."""
    registry = ToolRegistry(max_rationale_generations=0)
    assert _result(registry, "rm_rf", path="/") == {"error": "unknown tool 'rm_rf'"}


def test_bad_arguments_become_an_error_result(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "read_file", wrong_kwarg="x")
    assert "invalid arguments for read_file" in result["error"]


def test_unexpected_handler_failure_is_caught(
    chat_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a genuine bug in a handler must not end the turn."""

    def _boom() -> dict:
        raise RuntimeError("kaboom")

    registry = ToolRegistry(max_rationale_generations=0)
    monkeypatch.setitem(registry._handlers, "get_repo_overview", _boom)
    result = _result(registry, "get_repo_overview")
    assert "RuntimeError: kaboom" in result["error"]


def test_oversized_result_is_truncated_with_a_marker(
    chat_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _huge() -> dict:
        return {"blob": "x" * (MAX_RESULT_CHARS * 2)}

    registry = ToolRegistry(max_rationale_generations=0)
    monkeypatch.setitem(registry._handlers, "get_repo_overview", _huge)
    raw = registry.dispatch("get_repo_overview", {})
    assert raw.endswith(TRUNCATION_MARKER)
    assert len(raw) == MAX_RESULT_CHARS + len(TRUNCATION_MARKER)


def test_non_serializable_result_still_encodes(
    chat_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    monkeypatch.setitem(
        registry._handlers, "get_repo_overview", lambda: {"when": object()}
    )
    assert "when" in json.loads(registry.dispatch("get_repo_overview", {}))


# ---------------------------------------------------------------------------
# get_rationale: cache read, budgeted generation (§0.1)
# ---------------------------------------------------------------------------


def test_rationale_no_evidence_is_a_status_not_an_error(
    chat_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unscanned target yields status='no_evidence', not a failure."""
    monkeypatch.setattr(chat_tools, "collect_evidence", lambda target, limit=20: [])
    registry = ToolRegistry(max_rationale_generations=2)
    result = _result(registry, "get_rationale", qualified_name="pkg.a.run_turn")
    assert result["status"] == "no_evidence"
    assert result["target"]["qualified_name"] == "pkg.a.run_turn"


def _fake_evidence_item():
    """A stand-in for one ``CommitEvidence``.

    ``_format_response`` counts ``pull_requests`` / ``issues``, so the stub
    needs those two attributes and nothing else.
    """
    return SimpleNamespace(pull_requests=[], issues=[])


@pytest.fixture
def evidence_present(monkeypatch: pytest.MonkeyPatch):
    """Non-empty evidence, always a cache miss.

    The rationale budget is about *call accounting*, not about evidence
    collection (covered by the MCP suites), so the cheapest honest way to
    reach the budget branches is to stub the collector and the cache.
    """
    monkeypatch.setattr(
        chat_tools, "collect_evidence", lambda target, limit=20: [_fake_evidence_item()]
    )
    monkeypatch.setattr(chat_tools, "lookup_cached", lambda *a, **k: None)


def test_rationale_cache_miss_calls_the_mcp_tool_verbatim_once(
    chat_repo: Path, evidence_present, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero drift: the result *is* the MCP tool's return value."""
    calls: list[dict] = []
    sentinel = {"purpose": "p", "why": "w", "model": "m"}

    def _spy(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(chat_tools, "whygraph_rationale_brief", _spy)

    registry = ToolRegistry(max_rationale_generations=2)
    result = _result(registry, "get_rationale", qualified_name="pkg.a.run_turn")

    assert calls == [{"qualified_name": "pkg.a.run_turn"}]
    assert result["status"] == "cached"
    assert result["generated"] is True
    for key, value in sentinel.items():
        assert result[key] == value
    assert registry.generations_used == 1


def test_rationale_third_miss_in_one_turn_degrades_without_generating(
    chat_repo: Path, evidence_present, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #4: over budget → not_generated, and the spy is NOT invoked again."""
    calls: list[str] = []

    def _spy(*, qualified_name):
        calls.append(qualified_name)
        return {"purpose": "p"}

    monkeypatch.setattr(chat_tools, "whygraph_rationale_brief", _spy)

    registry = ToolRegistry(max_rationale_generations=2)
    for name in ("pkg.a.run_turn", "pkg.b.caller"):
        assert (
            _result(registry, "get_rationale", qualified_name=name)["generated"] is True
        )

    third = _result(registry, "get_rationale", qualified_name="src/pkg/a.py")
    assert third["status"] == "not_generated"
    assert third["note"] == "generation budget exhausted this turn"
    assert calls == ["pkg.a.run_turn", "pkg.b.caller"]  # never a third call
    assert registry.generations_used == 2


def test_rationale_zero_budget_never_generates(
    chat_repo: Path, evidence_present, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``max_rationale_generations = 0`` makes the tool cache-only."""

    def _spy(**kwargs):  # pragma: no cover -- must never run
        raise AssertionError("generation attempted with a zero budget")

    monkeypatch.setattr(chat_tools, "whygraph_rationale_brief", _spy)
    registry = ToolRegistry(max_rationale_generations=0)
    assert (
        _result(registry, "get_rationale", qualified_name="pkg.a.run_turn")["status"]
        == "not_generated"
    )


def test_rationale_cache_hit_never_generates(
    chat_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.analyze import Rationale

    monkeypatch.setattr(
        chat_tools, "collect_evidence", lambda target, limit=20: [_fake_evidence_item()]
    )
    monkeypatch.setattr(
        chat_tools,
        "lookup_cached",
        lambda *a, **k: (
            Rationale(
                purpose="cached purpose",
                why="w",
                constraints=(),
                tradeoffs=(),
                risks=(),
                model="m",
                provider="anthropic",
            ),
            "2026-07-29T00:00:00+00:00",
        ),
    )

    def _spy(**kwargs):  # pragma: no cover -- must never run
        raise AssertionError("generated on a cache hit")

    monkeypatch.setattr(chat_tools, "whygraph_rationale_brief", _spy)

    registry = ToolRegistry(max_rationale_generations=2)
    result = _result(registry, "get_rationale", qualified_name="pkg.a.run_turn")
    assert result["status"] == "cached"
    assert result["generated"] is False
    assert result["purpose"] == "cached purpose"
    assert registry.generations_used == 0


def test_budget_defaults_to_chat_config(
    chat_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        core, "_config", Config(chat=ChatConfig(max_rationale_generations=7))
    )
    assert ToolRegistry()._generation_budget == 7


# ---------------------------------------------------------------------------
# File tools: the clamp (§10, AC #6)
# ---------------------------------------------------------------------------


def test_read_file_returns_numbered_lines(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(
        registry, "read_file", path="src/pkg/a.py", start_line=2, end_line=4
    )
    assert result["content"] == "2→line 2\n3→line 3\n4→line 4"
    assert result["total_lines"] == 30
    assert result["truncated"] is True  # a slice, not the whole file


def test_read_file_whole_small_file_is_not_truncated(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "read_file", path="README.md")
    assert result["truncated"] is False
    assert result["content"] == "1→# hi"


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "src/../../outside.py",
    ],
)
def test_read_file_refuses_paths_outside_the_repo(chat_repo: Path, path: str) -> None:
    """AC #6: traversal and absolute paths are refused."""
    registry = ToolRegistry(max_rationale_generations=0)
    assert (
        "outside the repository" in _result(registry, "read_file", path=path)["error"]
    )


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("whygraph.toml", "live API keys"),
        (".env", "secrets"),
        (".env.local", "secrets"),
        (".whygraph/whygraph.db", "WhyGraph-internal"),
        (".git/config", "WhyGraph-internal"),
        ("src/../whygraph.toml", "live API keys"),
    ],
)
def test_read_file_refuses_deny_listed_paths(
    chat_repo: Path, path: str, reason: str
) -> None:
    """AC #6: inside-the-root is not enough — secrets stay unreadable."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "read_file", path=path)
    assert reason in result["error"]
    assert "SECRET" not in json.dumps(result)  # no key material in the transcript


def test_read_file_refuses_binary(chat_repo: Path) -> None:
    (chat_repo / "blob.bin").write_bytes(b"\x89PNG\x00\x01\x02")
    registry = ToolRegistry(max_rationale_generations=0)
    assert "binary" in _result(registry, "read_file", path="blob.bin")["error"]


def test_read_file_missing_and_directory_paths(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert "does not exist" in _result(registry, "read_file", path="nope.py")["error"]
    assert "is a directory" in _result(registry, "read_file", path="src")["error"]


def test_read_file_caps_the_line_range(chat_repo: Path) -> None:
    """A range wider than MAX_LINES is truncated, not refused."""
    from whygraph.chat import files

    big = "".join(f"row {n}\n" for n in range(1, files.MAX_LINES + 200))
    (chat_repo / "big.py").write_text(big, encoding="utf-8")

    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(
        registry, "read_file", path="big.py", start_line=1, end_line=10_000
    )
    assert result["end_line"] == files.MAX_LINES
    assert result["truncated"] is True


def test_read_file_clamps_a_zero_start_line(chat_repo: Path) -> None:
    """An off-by-one from the model shouldn't cost a tool round."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "read_file", path="README.md", start_line=0)
    assert result["start_line"] == 1


def test_read_file_start_past_end_of_file_errors(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "read_file", path="README.md", start_line=500)
    assert "past the end" in result["error"]


def test_read_file_respects_the_byte_cap(chat_repo: Path) -> None:
    """A one-line file bigger than the byte cap is cut, and the whole result
    is then cut again by the registry's own JSON truncation."""
    from whygraph.chat import files

    (chat_repo / "huge.py").write_text(
        "y" * (files.MAX_BYTES + 5_000), encoding="utf-8"
    )
    registry = ToolRegistry(max_rationale_generations=0)
    raw = registry.dispatch("read_file", {"path": "huge.py"})
    assert '"truncated": true' in raw
    assert raw.endswith(TRUNCATION_MARKER)
    assert len(raw) == MAX_RESULT_CHARS + len(TRUNCATION_MARKER)


# ---------------------------------------------------------------------------
# File tools: list_dir
# ---------------------------------------------------------------------------


def test_list_dir_marks_directories_and_hides_denied_entries(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    entries = _result(registry, "list_dir", path=".")["entries"]
    assert "src/" in entries
    assert "README.md" in entries
    # Deny-listed children are omitted, not listed-but-unreadable.
    assert ".git/" not in entries
    assert ".whygraph/" not in entries
    assert "whygraph.toml" not in entries
    assert ".env" not in entries


def test_list_dir_defaults_to_the_repo_root(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert "src/" in _result(registry, "list_dir")["entries"]


def test_list_dir_refuses_escapes_and_non_directories(chat_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert (
        "outside the repository" in _result(registry, "list_dir", path="../..")["error"]
    )
    assert "not a directory" in _result(registry, "list_dir", path="README.md")["error"]
    assert "does not exist" in _result(registry, "list_dir", path="nope")["error"]


def test_list_dir_caps_entry_count(chat_repo: Path) -> None:
    from whygraph.chat import files

    many = chat_repo / "many"
    many.mkdir()
    for n in range(files.MAX_ENTRIES + 10):
        (many / f"f{n:04d}.txt").write_text("x", encoding="utf-8")

    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "list_dir", path="many")
    assert len(result["entries"]) == files.MAX_ENTRIES
    assert result["truncated"] is True
