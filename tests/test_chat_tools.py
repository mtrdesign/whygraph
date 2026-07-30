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


def test_fifteen_tools_with_unique_names_and_object_schemas() -> None:
    names = [spec.name for spec in TOOL_SPECS]
    assert len(names) == 15
    assert len(set(names)) == 15
    assert set(names) == {
        "search_symbols",
        "get_symbol",
        "get_area_outline",
        "get_rationale",
        "get_evidence",
        "get_area_history",
        "find_changes",
        "get_commit",
        "get_pr",
        "get_issue",
        "get_repo_overview",
        "list_recent_activity",
        "run_project_stats",
        "read_file",
        "list_dir",
    }
    for spec in TOOL_SPECS:
        assert spec.parameters["type"] == "object"
        assert spec.description  # the model's only guidance


def test_symbol_tools_document_the_real_qualified_name_shapes() -> None:
    """Regression guard on a costly documentation bug.

    All three symbol-keyed tools used to describe ``qualified_name`` as a
    "Dotted symbol name". CodeGraph uses bare names, ``Class::method``, and
    file paths — never a dotted symbol path — so the model followed the
    description, missed every lookup, and burned tool rounds guessing.
    """
    keyed = ["get_symbol", "get_rationale", "get_evidence"]
    for name in keyed:
        spec = next(s for s in TOOL_SPECS if s.name == name)
        description = spec.parameters["properties"]["qualified_name"]["description"]
        assert "::" in description, f"{name} must document the method shape"
        assert "not valid" in description.lower(), (
            f"{name} must warn that a dotted path does not resolve"
        )
        assert "dotted symbol name" not in description.lower()


def test_history_tools_state_which_description_field_is_authoritative() -> None:
    """Regression guard on the field-authority fix (§4.2).

    ``llm_description`` is generated from the diff alone, so a nonsense commit
    message cannot contaminate it — but nothing used to tell the model that, so
    it read ``subject`` as equally authoritative and anchored on whichever
    field arrived first.
    """
    from whygraph.chat.tools import _DESCRIPTION_AUTHORITY

    history = [
        "get_evidence",
        "get_area_history",
        "find_changes",
        "get_commit",
        "list_recent_activity",
    ]
    for name in history:
        spec = next(s for s in TOOL_SPECS if s.name == name)
        assert _DESCRIPTION_AUTHORITY in spec.description, (
            f"{name} must state that llm_description is the reliable field"
        )
    assert "DIFF ALONE" in _DESCRIPTION_AUTHORITY
    assert "terse, stale, or simply wrong" in _DESCRIPTION_AUTHORITY


def test_the_stats_spec_ships_the_annotated_schema() -> None:
    """The shipped description *is* the schema doc — not a paraphrase of it.

    Detailed content assertions live in ``test_chat_stats_sql.py``; this guards
    the wiring, which is the part that could silently regress to a short
    summary "to save tokens".
    """
    from whygraph.chat.stats_sql import _SCHEMA_DOC

    spec = next(s for s in TOOL_SPECS if s.name == "run_project_stats")
    assert spec.description == _SCHEMA_DOC
    assert "FOUR REQUIRED RULES" in spec.description
    assert "=== TABLES ===" in spec.description


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
# get_area_outline (the package-shaped query CodeGraph has no node kind for)
# ---------------------------------------------------------------------------


def _node(
    node_id: str,
    kind: str,
    name: str,
    file_path: str,
    start_line: int = 1,
    *,
    qualified_name: str | None = None,
) -> dict:
    """One ``nodes`` row for the outline fixtures."""
    return {
        "id": node_id,
        "kind": kind,
        "name": name,
        "qualified_name": qualified_name or name,
        "file_path": file_path,
        "language": "python",
        "start_line": start_line,
        "end_line": start_line + 5,
        "docstring": None,
        "signature": f"def {name}()",
    }


# Two sibling directories whose names differ only where a LIKE wildcard would
# match: `pkg_a` vs `pkgXa`. `_` is a single-character wildcard, so an
# unescaped prefix query for `pkg_a` returns both.
_OUTLINE_NODES = [
    _node(
        "o_file",
        "file",
        "one.py",
        "src/pkg_a/one.py",
        1,
        qualified_name="src/pkg_a/one.py",
    ),
    # Deliberately out of line order in the fixture — `area()` must sort.
    _node("o_beta", "function", "beta", "src/pkg_a/one.py", 30),
    _node("o_alpha", "function", "alpha", "src/pkg_a/one.py", 10),
    _node("o_cls", "class", "Widget", "src/pkg_a/two.py", 4),
    _node("o_meth", "method", "Widget::run", "src/pkg_a/two.py", 8),
    # Noise kinds the outline must drop.
    _node("o_imp", "import", "json", "src/pkg_a/one.py", 2),
    _node("o_var", "variable", "COUNTER", "src/pkg_a/one.py", 3),
    # The wildcard trap.
    _node("o_other", "function", "intruder", "src/pkgXa/three.py", 1),
]


@pytest.fixture
def outline_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codegraph_db_factory):
    """A repo whose CodeGraph index has two look-alike sibling packages."""
    root = tmp_path / "outline"
    (root / ".git").mkdir(parents=True)
    cg_path = codegraph_db_factory(nodes=_OUTLINE_NODES, edges=[])
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


@pytest.mark.parametrize(
    "path",
    ["src/pkg_a", "src/pkg_a/", "./src/pkg_a", "./src/pkg_a/"],
)
def test_outline_accepts_every_spelling_of_a_directory(
    outline_repo: Path, path: str
) -> None:
    """§2.1's failure was six directory lookups; all four spellings must work."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_area_outline", path=path)
    assert result["detail"] == "symbols"
    assert set(result["files"]) == {"src/pkg_a/one.py", "src/pkg_a/two.py"}


def test_outline_of_an_exact_file_path_returns_just_that_file(
    outline_repo: Path,
) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_area_outline", path="src/pkg_a/two.py")
    assert set(result["files"]) == {"src/pkg_a/two.py"}
    assert [s["name"] for s in result["files"]["src/pkg_a/two.py"]["symbols"]] == [
        "Widget",
        "Widget::run",
    ]


def test_outline_escapes_like_wildcards_in_the_prefix(outline_repo: Path) -> None:
    """Risk 3: `_` is a single-char wildcard and this repo's paths are full of it."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_area_outline", path="src/pkg_a")
    assert "src/pkgXa/three.py" not in result["files"]
    assert "intruder" not in json.dumps(result)


def test_outline_drops_noise_kinds_and_orders_by_line(outline_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    entry = _result(registry, "get_area_outline", path="src/pkg_a")["files"][
        "src/pkg_a/one.py"
    ]
    # `import` and `variable` are excluded; the file node itself is kept.
    assert [s["name"] for s in entry["symbols"]] == ["one.py", "alpha", "beta"]
    assert [s["start_line"] for s in entry["symbols"]] == [1, 10, 30]


def test_outline_omits_signature_and_id(outline_repo: Path) -> None:
    """Decision §0.1 #1 — signatures are 75% of payload cost; get_symbol has them."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_area_outline", path="src/pkg_a")
    for entry in result["files"].values():
        for symbol in entry["symbols"]:
            assert "signature" not in symbol
            assert "id" not in symbol
            # `file_path` is the key of the map this row sits under.
            assert "file_path" not in symbol
            assert set(symbol) == {
                "qualified_name",
                "name",
                "kind",
                "start_line",
                "end_line",
            }


def test_outline_degrades_to_a_file_map_past_the_symbol_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, codegraph_db_factory
) -> None:
    """AC 2: a truncated list hides what was missed; a map redirects the next call."""
    from whygraph.chat.tools import _OUTLINE_SYMBOL_LIMIT

    nodes = [
        _node(f"big_{n}", "function", f"fn{n}", f"src/big/mod{n % 7}.py", n + 1)
        for n in range(_OUTLINE_SYMBOL_LIMIT + 1)
    ]
    root = tmp_path / "big"
    (root / ".git").mkdir(parents=True)
    cg_path = codegraph_db_factory(nodes=nodes, edges=[])
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        core, "_config", Config(whygraph_db=root / "wg.db", codegraph_db=cg_path)
    )
    db_engine._reset_engine()
    try:
        registry = ToolRegistry(max_rationale_generations=0)
        result = _result(registry, "get_area_outline", path="src/big")
        assert result["detail"] == "files"
        assert result["symbol_count"] == _OUTLINE_SYMBOL_LIMIT + 1
        assert "re-call on a subdirectory" in result["hint"]
        assert len(result["files"]) == 7
        for entry in result["files"].values():
            assert "symbols" not in entry  # a map, not a truncated listing
            assert entry["symbol_count"] > 0
        assert len(registry.dispatch("get_area_outline", {"path": "src/big"})) < 8_000
    finally:
        db_engine._reset_engine()
        core._reset_config()


def test_outline_unknown_path_explains_the_index_coverage_gap(
    outline_repo: Path,
) -> None:
    """Markdown and TOML are not indexed at all — say so instead of returning {}."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_area_outline", path="docs")
    assert result["symbol_count"] == 0
    assert result["files"] == {}
    assert "list_dir" in result["note"]


def test_outline_requires_a_path(outline_repo: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert (
        "path is required" in _result(registry, "get_area_outline", path="  ")["error"]
    )


def test_outline_omits_commit_counts_when_whygraph_is_unscanned(
    outline_repo: Path,
) -> None:
    """AC 4: the structural answer is useful alone, so a missing DB degrades."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_area_outline", path="src/pkg_a")
    assert result["detail"] == "symbols"
    for entry in result["files"].values():
        assert "commit_count" not in entry


def test_outline_carries_per_file_commit_counts(
    outline_repo: Path,
) -> None:
    """AC 4: CodeGraph results travel with their WhyGraph handles (§4.1c)."""
    from alembic import command

    from whygraph.db import get_session
    from whygraph.db.bootstrap import alembic_config
    from whygraph.db.models import Commit, CommitFileChange

    command.upgrade(alembic_config(), "head")
    with get_session() as session:
        for n, (sha, on_main) in enumerate([("s1", 1), ("s2", 1), ("s3", 0)]):
            session.add(
                Commit(
                    sha=sha,
                    parent_shas="",
                    author_name="dev",
                    author_email="dev@example.com",
                    authored_at=f"2026-07-0{n + 1}T00:00:00+00:00",
                    committed_at=f"2026-07-0{n + 1}T00:00:00+00:00",
                    subject=f"subject {sha}",
                    body="",
                    files_changed=1,
                    insertions=1,
                    deletions=0,
                    scanned_at="2026-07-30T00:00:00+00:00",
                    on_default_branch=on_main,
                )
            )
            session.add(
                CommitFileChange(
                    commit_sha=sha,
                    path="src/pkg_a/one.py",
                    change_type="M",
                    lines_added=1,
                    lines_deleted=0,
                )
            )
        # A commit on the look-alike sibling must not leak into pkg_a's counts.
        session.add(
            CommitFileChange(
                commit_sha="s1",
                path="src/pkgXa/three.py",
                change_type="M",
                lines_added=1,
                lines_deleted=0,
            )
        )
        session.commit()

    registry = ToolRegistry(max_rationale_generations=0)
    files = _result(registry, "get_area_outline", path="src/pkg_a")["files"]
    # s3 is off the default branch, so it is not counted (same rule as area-history).
    assert files["src/pkg_a/one.py"]["commit_count"] == 2
    # Scanned but untouched reports 0 — distinguishable from "no DB", which omits.
    assert files["src/pkg_a/two.py"]["commit_count"] == 0


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


# ---------------------------------------------------------------------------
# list_recent_activity (the "what shipped lately" entry point)
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_history(chat_repo: Path):
    """A migrated WhyGraph DB with commits, a PR, and an issue.

    ``chat_repo`` points config at an *empty* DB file; this migrates it and
    seeds enough rows to assert ordering, the default-branch filter, and the
    per-category cap.
    """
    from alembic import command

    from whygraph.db import get_session
    from whygraph.db.bootstrap import alembic_config
    from whygraph.db.models import Commit, Issue, PullRequest

    command.upgrade(alembic_config(), "head")
    with get_session() as session:
        for n, (sha, day, on_main) in enumerate(
            [
                ("aaa", "2026-07-01", 1),
                ("bbb", "2026-07-03", 1),
                ("ccc", "2026-07-05", 1),
                # A PR-origin commit recovered from a squash merge: newest of
                # all, and must NOT appear (same rule area-history follows).
                ("ddd", "2026-07-09", 0),
            ]
        ):
            session.add(
                Commit(
                    sha=sha,
                    parent_shas="",
                    author_name=f"dev{n}",
                    author_email=f"dev{n}@example.com",
                    authored_at=f"{day}T00:00:00+00:00",
                    committed_at=f"{day}T00:00:00+00:00",
                    subject=f"subject {sha}",
                    body="",
                    files_changed=1,
                    insertions=2,
                    deletions=3,
                    scanned_at="2026-07-30T00:00:00+00:00",
                    llm_description="D" * 500 if sha == "ccc" else None,
                    on_default_branch=on_main,
                )
            )
        session.add(
            PullRequest(
                number=7,
                title="the PR",
                state="closed",
                created_at="2026-07-02T00:00:00+00:00",
                updated_at="2026-07-06T00:00:00+00:00",
                merged_at="2026-07-06T00:00:00+00:00",
                head_sha="bbb",
                base_ref="main",
                author="dev1",
                html_url="https://example.invalid/pr/7",
                labels="[]",
                fetched_at="2026-07-30T00:00:00+00:00",
            )
        )
        session.add(
            Issue(
                number=3,
                title="the issue",
                state="open",
                created_at="2026-07-01T00:00:00+00:00",
                updated_at="2026-07-04T00:00:00+00:00",
                author="dev0",
                html_url="https://example.invalid/issue/3",
                labels="[]",
                fetched_at="2026-07-30T00:00:00+00:00",
            )
        )
        session.commit()
    return chat_repo


def test_recent_activity_returns_all_three_categories_newest_first(
    seeded_history: Path,
) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "list_recent_activity")

    # Newest first, and the off-main squash-merge commit is excluded.
    assert [c["sha"] for c in result["commits"]] == ["ccc", "bbb", "aaa"]
    assert [p["number"] for p in result["pull_requests"]] == [7]
    assert [i["number"] for i in result["issues"]] == [3]
    # One call answers "what shipped lately" — that is the whole point.
    assert result["commits"][0]["subject"] == "subject ccc"
    assert result["pull_requests"][0]["merged_at"].startswith("2026-07-06")


def test_recent_activity_caps_each_category_and_clamps_a_bad_limit(
    seeded_history: Path,
) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert len(_result(registry, "list_recent_activity", limit=2)["commits"]) == 2
    # limit is per category, not a total, and 0/negative clamps to 1.
    assert len(_result(registry, "list_recent_activity", limit=0)["commits"]) == 1


def test_recent_activity_truncates_long_descriptions(seeded_history: Path) -> None:
    """A dozen full paragraphs would cost more context than it saves."""
    from whygraph.mcp.resources import _RECENT_DESCRIPTION_CHARS

    registry = ToolRegistry(max_rationale_generations=0)
    newest = _result(registry, "list_recent_activity")["commits"][0]
    assert newest["description"].endswith("…")
    assert len(newest["description"]) == _RECENT_DESCRIPTION_CHARS + 1


def test_recent_activity_empty_categories_are_lists_not_omitted(
    chat_repo: Path,
) -> None:
    """ "None scanned" must be distinguishable from "not returned"."""
    from alembic import command

    from whygraph.db.bootstrap import alembic_config

    command.upgrade(alembic_config(), "head")
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "list_recent_activity")
    assert result["commits"] == []
    assert result["pull_requests"] == []
    assert result["issues"] == []


def test_recent_activity_on_an_unscanned_db_is_a_result_not_an_exception(
    chat_repo: Path,
) -> None:
    """Dispatch discipline: a missing DB can't end the user's turn."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "list_recent_activity")
    assert "error" in result
    assert "scan" in result["error"]


# ---------------------------------------------------------------------------
# find_changes (content search over diff descriptions — the debugging entry)
# ---------------------------------------------------------------------------


@pytest.fixture
def searchable_history(chat_repo: Path):
    """History shaped to exercise every ``find_changes`` filter.

    The interesting rows are the two that are *only* reachable through the new
    haystacks: ``squash`` says nothing useful in its own text and is findable
    only via its PR title, and ``old`` lives under a pre-rename path.
    """
    from alembic import command

    from whygraph.db import get_session
    from whygraph.db.bootstrap import alembic_config
    from whygraph.db.models import Commit, CommitFileChange, PullRequest

    command.upgrade(alembic_config(), "head")

    def _commit(sha, day, *, description, subject="a change", on_main=1, body=""):
        return Commit(
            sha=sha,
            parent_shas="",
            author_name="dev",
            author_email="dev@example.com",
            authored_at=f"2026-07-{day}T00:00:00+00:00",
            committed_at=f"2026-07-{day}T00:00:00+00:00",
            subject=subject,
            body=body,
            files_changed=1,
            insertions=1,
            deletions=0,
            scanned_at="2026-07-30T00:00:00+00:00",
            llm_description=description,
            on_default_branch=on_main,
        )

    with get_session() as session:
        session.add_all(
            [
                _commit(
                    "hit",
                    "05",
                    description="Fixed the DROPDOWN so the session survives a refresh.",
                ),
                _commit("miss", "04", description="Renamed a private helper."),
                # Its own text is useless — only the PR title describes it.
                _commit("squash", "06", description="Bulk edit.", subject="squash!"),
                # Off the main walk: matches the keyword but must not be returned.
                _commit(
                    "offmain",
                    "07",
                    description="Another dropdown tweak.",
                    on_main=0,
                ),
                # The keyword lives in an underscore-bearing token.
                _commit("under", "03", description="Touched pkg_a during the sweep."),
                _commit("old", "01", description="Created the original module."),
                _commit("renamer", "02", description="Moved the module."),
            ]
        )
        for sha, path in [
            ("hit", "src/playground/src/api.ts"),
            ("miss", "src/whygraph/core/config.py"),
            ("squash", "src/playground/src/api.ts"),
            ("under", "src/pkg_a/one.py"),
            ("old", "src/whygraph/legacy.py"),
        ]:
            session.add(
                CommitFileChange(
                    commit_sha=sha,
                    path=path,
                    change_type="M",
                    lines_added=1,
                    lines_deleted=0,
                )
            )
        # The rename edge `resolve_path_aliases` walks backwards.
        session.add(
            CommitFileChange(
                commit_sha="renamer",
                path="src/whygraph/current.py",
                change_type="R",
                renamed_from="src/whygraph/legacy.py",
                similarity=98,
                lines_added=0,
                lines_deleted=0,
            )
        )
        session.add(
            PullRequest(
                number=38,
                title="fix(serve): stop the provider dropdown resetting mid-session",
                body="A long discussion mentioning quicksort and other irrelevancies.",
                state="closed",
                created_at="2026-07-05T00:00:00+00:00",
                updated_at="2026-07-06T00:00:00+00:00",
                merged_at="2026-07-06T00:00:00+00:00",
                merge_commit_sha="squash",
                head_sha="squash",
                base_ref="main",
                author="dev",
                html_url="https://example.invalid/pr/38",
                labels="[]",
                commit_titles="[]",
                comments="[]",
                fetched_at="2026-07-30T00:00:00+00:00",
            )
        )
        session.commit()
    return chat_repo


def _shas(registry: ToolRegistry, **kwargs) -> list[str]:
    return [c["sha"] for c in _result(registry, "find_changes", **kwargs)["commits"]]


def test_find_changes_matches_description_content_case_insensitively(
    searchable_history: Path,
) -> None:
    """AC 5: the case search_symbols cannot answer at all."""
    registry = ToolRegistry(max_rationale_generations=0)
    assert "hit" in _shas(registry, query="dropdown")
    assert "hit" in _shas(registry, query="DROPDOWN")


def test_find_changes_ands_every_term(searchable_history: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert _shas(registry, query="dropdown refresh") == ["hit"]
    # Both terms must appear in the same commit.
    assert _shas(registry, query="dropdown quicksort") == []


def test_find_changes_requires_a_filter(searchable_history: Path) -> None:
    """Without one this would dump the whole history."""
    registry = ToolRegistry(max_rationale_generations=0)
    assert "query" in _result(registry, "find_changes")["error"]


def test_find_changes_matches_on_a_linked_pr_title_alone(
    searchable_history: Path,
) -> None:
    """AC 6: for a squash merge the PR title is often the only real summary."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "find_changes", query="resetting")
    assert [c["sha"] for c in result["commits"]] == ["squash"]
    # And the PR that explains the match travels with the row.
    assert result["commits"][0]["linked_prs"] == [
        {
            "number": 38,
            "title": "fix(serve): stop the provider dropdown resetting mid-session",
        }
    ]


def test_find_changes_ignores_pr_bodies(searchable_history: Path) -> None:
    """Decision §0.1 #3: titles are 1,967 chars across the repo, bodies 77,420."""
    registry = ToolRegistry(max_rationale_generations=0)
    assert _shas(registry, query="quicksort") == []


def test_find_changes_excludes_off_default_branch_commits(
    searchable_history: Path,
) -> None:
    """Squash-recovered PR-origin commits would double-count the same work."""
    registry = ToolRegistry(max_rationale_generations=0)
    assert "offmain" not in _shas(registry, query="dropdown")


def test_find_changes_accepts_a_directory_where_area_history_cannot(
    searchable_history: Path,
) -> None:
    """AC 7: get_area_history returns 0 for the same input, on purpose."""
    registry = ToolRegistry(max_rationale_generations=0)
    assert _shas(registry, path="src/playground/src") == ["squash", "hit"]
    unchanged = _result(registry, "get_area_history", path="src/playground/src")
    assert unchanged["evidence"] == []


def test_find_changes_follows_a_rename_chain(searchable_history: Path) -> None:
    """AC 8: a bare `WHERE path = ?` silently loses pre-rename history."""
    registry = ToolRegistry(max_rationale_generations=0)
    shas = _shas(registry, path="src/whygraph/current.py")
    assert "old" in shas, "the pre-rename commit must be reachable"
    assert "renamer" in shas


def test_find_changes_reports_which_paths_matched(searchable_history: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    rows = _result(registry, "find_changes", path="src/whygraph/current.py")["commits"]
    by_sha = {r["sha"]: r for r in rows}
    # The alias, not the queried name — that is the evidence the rename worked.
    assert by_sha["old"]["matched_paths"] == ["src/whygraph/legacy.py"]
    # No path filter → no matched_paths key at all.
    assert (
        "matched_paths"
        not in _result(registry, "find_changes", query="dropdown")["commits"][0]
    )


def test_find_changes_escapes_like_wildcards_in_a_term(
    searchable_history: Path,
) -> None:
    """Risk 3: `pkg_a` must not match `pkgXa` via the `_` wildcard."""
    registry = ToolRegistry(max_rationale_generations=0)
    assert _shas(registry, query="pkg_a") == ["under"]
    assert _shas(registry, query="pkgXa") == []


def test_find_changes_omits_the_heavy_blobs(searchable_history: Path) -> None:
    """AC 9: pr.body / pr.comments / commit.body are 73% of what truncates today."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "find_changes", query="resetting")
    blob = json.dumps(result)
    assert "quicksort" not in blob  # the PR body is nowhere in the payload
    row = result["commits"][0]
    for key in ("body", "comments", "commit_titles"):
        assert key not in row
    assert set(row["linked_prs"][0]) == {"number", "title"}


def test_find_changes_emits_the_description_in_full(searchable_history: Path) -> None:
    """Clipping the description defeats the entire purpose of the tool."""
    from whygraph.mcp.resources import _RECENT_DESCRIPTION_CHARS

    from whygraph.db import get_session
    from whygraph.db.models import Commit

    long_text = "Sessions vanish. " + "detail " * 200
    assert len(long_text) > _RECENT_DESCRIPTION_CHARS
    with get_session() as session:
        commit = session.get(Commit, "hit")
        commit.llm_description = long_text
        session.add(commit)
        session.commit()

    registry = ToolRegistry(max_rationale_generations=0)
    row = _result(registry, "find_changes", query="vanish")["commits"][0]
    assert row["llm_description"] == long_text


def test_find_changes_clamps_the_limit(searchable_history: Path) -> None:
    from whygraph.mcp.resources import _FIND_CHANGES_MAX_LIMIT

    registry = ToolRegistry(max_rationale_generations=0)
    assert len(_shas(registry, path="src", limit=1)) == 1
    # A model asking for 500 gets the cap, not a payload truncated mid-JSON.
    assert _result(registry, "find_changes", path="src", limit=500)["count"] <= (
        _FIND_CHANGES_MAX_LIMIT
    )
    assert len(_shas(registry, path="src", limit=0)) == 1


def test_find_changes_stays_parseable_at_its_own_max_limit(
    searchable_history: Path,
) -> None:
    """Regression: a row-count cap does NOT bound the payload.

    Shipped wrong once. Descriptions are emitted in full and average ~1,280
    chars, so ``limit=30`` produced 30,014 chars against a 30,000-char registry
    cap — truncated mid-string, and therefore **invalid JSON the model could not
    read at all**. Exactly the failure this tool exists to avoid.
    """
    from sqlmodel import select

    from whygraph.db import get_session
    from whygraph.db.models import Commit

    # Give every commit a description far longer than any real one.
    with get_session() as session:
        for commit in session.exec(select(Commit)).all():
            commit.llm_description = "Sessions vanish. " + ("detail " * 900)
            session.add(commit)
        session.commit()

    registry = ToolRegistry(max_rationale_generations=0)
    raw = registry.dispatch("find_changes", {"query": "vanish", "limit": 50})
    assert not raw.endswith(TRUNCATION_MARKER), "must never truncate mid-JSON"
    result = json.loads(raw)  # the assertion that actually matters
    assert len(raw) < MAX_RESULT_CHARS
    # At least one row survives, and the shortfall is stated rather than implied.
    assert result["count"] >= 1
    assert result["omitted"] >= 1
    assert "get_commit" in result["note"]


def test_find_changes_omits_nothing_when_everything_fits(
    searchable_history: Path,
) -> None:
    """The counterpart: no spurious `omitted` key on a small result."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "find_changes", query="dropdown")
    assert "omitted" not in result
    assert "note" not in result


def test_find_changes_on_an_unscanned_db_is_a_result_not_an_exception(
    chat_repo: Path,
) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "find_changes", query="anything")
    assert "scan" in result["error"]
