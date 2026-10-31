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
from whygraph.db import ensure_initialized

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


@pytest.fixture(autouse=True)
def _migrated_for_rationale(request: pytest.FixtureRequest) -> None:
    """``get_rationale`` reads ``project_note``; the other tests want a bare DB."""
    if "chat_repo" in request.fixturenames and request.node.name.startswith(
        "test_rationale"
    ):
        request.getfixturevalue("chat_repo")
        ensure_initialized()


def _result(registry: ToolRegistry, name: str, **arguments) -> dict:
    """Dispatch and JSON-decode, so tests assert on structure not strings."""
    return json.loads(registry.dispatch(name, arguments))


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def test_twenty_three_tools_with_unique_names_and_object_schemas() -> None:
    names = [spec.name for spec in TOOL_SPECS]
    assert len(names) == 23
    assert len(set(names)) == 23
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
        "get_work_in_flight",
        "get_area_owners",
        "compare_refs",
        "get_milestones",
        "run_project_stats",
        "run_graph_stats",
        "render_chart",
        "read_file",
        "list_dir",
        "find_notes",
        "propose_note",
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
    from whygraph.mcp.descriptions import DESCRIPTION_AUTHORITY

    history = [
        "get_evidence",
        "get_area_history",
        "find_changes",
        "get_commit",
        "list_recent_activity",
    ]
    for name in history:
        spec = next(s for s in TOOL_SPECS if s.name == name)
        assert DESCRIPTION_AUTHORITY in spec.description, (
            f"{name} must state that llm_description is the reliable field"
        )
    assert "DIFF ALONE" in DESCRIPTION_AUTHORITY
    assert "terse, stale, or simply wrong" in DESCRIPTION_AUTHORITY


def test_the_stats_spec_ships_the_annotated_schema() -> None:
    """The shipped description *is* the schema doc — not a paraphrase of it.

    Detailed content assertions live in ``test_chat_stats_sql.py``; this guards
    the wiring, which is the part that could silently regress to a short
    summary "to save tokens".
    """
    from whygraph.chat.stats_sql import _SCHEMA_DOC

    spec = next(s for s in TOOL_SPECS if s.name == "run_project_stats")
    assert spec.description == _SCHEMA_DOC
    assert "FIVE REQUIRED RULES" in spec.description
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


def test_dispatch_outcome_classifies_each_ending(
    chat_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.mcp.errors import WhyGraphError

    def _typeerror() -> dict:
        raise TypeError("nope")

    def _whygraph() -> dict:
        raise WhyGraphError("not found", code="not_found")

    def _boom() -> dict:
        raise RuntimeError("kaboom")

    registry = ToolRegistry(max_rationale_generations=0)
    handlers = registry._handlers
    outcomes = {}
    assert registry.dispatch_outcome("nope", {})[1] == "error"
    for label, fn in (
        ("type", _typeerror),
        ("why", _whygraph),
        ("boom", _boom),
        ("dict", lambda: {"error": "soft"}),
        ("fine", lambda: {"ok": 1}),
    ):
        monkeypatch.setitem(handlers, "get_repo_overview", fn)
        text, outcome = registry.dispatch_outcome("get_repo_overview", {})
        assert text == registry.dispatch("get_repo_overview", {})
        outcomes[label] = outcome
    assert outcomes == {
        "type": "error",
        "why": "error",
        "boom": "internal",
        "dict": "error",
        "fine": "ok",
    }


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


def test_chat_reads_the_full_evidence_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat keeps ``detail="full"``: it truncates results itself (plan 5.4)."""
    seen: list[dict] = []

    def _spy(**kwargs: object) -> dict:
        seen.append(kwargs)
        return {"evidence": []}

    monkeypatch.setattr(chat_tools, "whygraph_evidence_for", _spy)
    monkeypatch.setattr(chat_tools, "whygraph_area_history", _spy)
    chat_tools._get_evidence("run_turn")
    chat_tools._get_area_history("a.py")
    assert [call["detail"] for call in seen] == ["full", "full"]


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


# ---------------------------------------------------------------------------
# Charting — ref minting, and the payload discipline the split design buys
# ---------------------------------------------------------------------------


_CHART_RESULT = {
    "sql": "SELECT strftime('%Y-%m', authored_at) AS month, count(*) AS commits …",
    "columns": ["month", "commits"],
    "rows": [["2026-03", 41], ["2026-04", 38], ["2026-05", 52]],
    "row_count": 3,
    "truncated": False,
}


def _minted(**overrides) -> tuple[ToolRegistry, dict]:
    """Run one synthetic aggregate result through the minting policy."""
    registry = ToolRegistry(max_rationale_generations=0)
    result = {**_CHART_RESULT, **overrides}
    return registry, registry._mint_chart_ref(result)


def test_a_chartable_result_carries_an_opaque_ref_and_an_invitation() -> None:
    """`columns` and `chart_ref` arrive together — the defense against
    hallucinated column names, since the names are on screen in the message
    immediately before the render_chart call."""
    _, result = _minted()
    assert result["chart_ref"].startswith("cr_")
    assert result["columns"] == ["month", "commits"]
    assert "render_chart" in result["chartable"]


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("errored", {"error": "nope", "layer": "shape"}),
        ("truncated", {"truncated": True}),
        ("one row", {"rows": [["2026-03", 41]], "row_count": 1}),
        ("no rows", {"rows": [], "row_count": 0}),
    ],
)
def test_no_ref_is_minted_for_a_result_not_worth_charting(
    label: str, overrides: dict
) -> None:
    """Withholding the affordance beats refusing later.

    A truncated result is the important one: the tool cannot know the true total,
    so a chart drawn from it would be a confident lie.
    """
    _, result = _minted(**overrides)
    assert "chart_ref" not in result, label
    assert "chartable" not in result, label


def test_two_rows_are_chartable_because_a_two_bar_chart_is_legitimate() -> None:
    """The minting floor is the cheapest kind's minimum, not a flat 3.

    A higher floor would make `bar` unreachable on a two-row result — "which of
    these two developers has been busier" is a real question.
    """
    _, result = _minted(rows=[["alice", 12], ["bob", 9]], row_count=2)
    assert "chart_ref" in result


def test_refs_are_unique_unguessable_and_not_sequential() -> None:
    """Risk 18: a ref must not address anything the model did not just compute."""
    registry = ToolRegistry(max_rationale_generations=0)
    refs = [
        registry._mint_chart_ref(dict(_CHART_RESULT))["chart_ref"] for _ in range(8)
    ]
    assert len(set(refs)) == 8
    for index, ref in enumerate(refs):
        assert len(ref) == len("cr_") + 8  # token_hex(4)
        assert all(char in "0123456789abcdef" for char in ref[3:])
        # A counter would be guessable, and would let the model address a result
        # from a turn it never saw.
        assert ref != f"cr_{index:08x}"


def test_render_chart_returns_the_directive_but_never_the_rows() -> None:
    """§5.4's payload discipline, and the answer to risk 1.

    The frontend already has the rows from the producer's result and correlates
    on `chart_ref`, so echoing them would double a 200-row payload for nothing —
    and truncation mid-array is what makes an oversized payload unparseable.
    """
    registry, produced = _minted()
    result = _result(
        registry,
        "render_chart",
        chart_ref=produced["chart_ref"],
        kind="line",
        title="Commits per month",
        x="month",
        y="commits",
    )
    assert result["chart"]["x_index"] == 0
    assert result["chart"]["y_index"] == 1
    assert result["columns"] == ["month", "commits"]
    assert result["row_count"] == 3
    assert "rows" not in result


def test_an_unknown_ref_is_a_readable_error_not_an_exception() -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(
        registry,
        "render_chart",
        chart_ref="cr_deadbeef",
        kind="bar",
        title="t",
        x="month",
        y="commits",
    )
    assert result["layer"] == "ref"
    assert "Re-run the query" in result["error"]


def test_a_ref_from_another_registry_is_rejected() -> None:
    """Refs die with the turn, which is what makes `build_window`'s eliding of
    older tool results irrelevant to charting rather than something to work
    around."""
    _, produced = _minted()
    other = ToolRegistry(max_rationale_generations=0)
    result = _result(
        other,
        "render_chart",
        chart_ref=produced["chart_ref"],
        kind="bar",
        title="t",
        x="month",
        y="commits",
    )
    assert result["layer"] == "ref"


def test_a_bad_column_is_a_chart_error_naming_the_available_ones() -> None:
    registry, produced = _minted()
    result = _result(
        registry,
        "render_chart",
        chart_ref=produced["chart_ref"],
        kind="bar",
        title="t",
        x="week",
        y="commits",
    )
    assert result["layer"] == "chart"
    assert "'month'" in result["error"] and "'commits'" in result["error"]


def test_a_stacked_directive_round_trips_through_dispatch() -> None:
    """The whole stacked path, as the model actually reaches it."""
    registry = ToolRegistry(max_rationale_generations=0)
    produced = registry._mint_chart_ref(
        {
            "columns": ["month", "kind", "n"],
            "rows": [
                ["2026-03", "M", 40],
                ["2026-03", "A", 12],
                ["2026-04", "M", 30],
            ],
            "row_count": 3,
            "truncated": False,
        }
    )
    result = _result(
        registry,
        "render_chart",
        chart_ref=produced["chart_ref"],
        kind="bar_stacked",
        title="Changes per month by type",
        x="month",
        y="n",
        series="kind",
    )
    chart = result["chart"]
    assert chart["series_values"] == ["M", "A"]
    assert chart["cells"]["2026-04"]["A"] == 0  # densified, and counted:
    assert chart["filled_cells"] == 1


def test_run_project_stats_has_no_chart_parameter() -> None:
    """There is exactly one way to draw a chart.

    An earlier design put `chart` on the producer; it was removed so charting is
    a capability rather than a parameter that every future producer must grow.
    """
    for name in ("run_project_stats", "run_graph_stats"):
        spec = next(s for s in TOOL_SPECS if s.name == name)
        assert set(spec.parameters["properties"]) == {"sql"}


def test_the_render_chart_spec_tells_the_model_to_name_columns() -> None:
    """A wording guard, mirroring `DESCRIPTION_AUTHORITY`.

    "Name columns, never values" is the one instruction that keeps a chart
    traceable to the query that produced it.
    """
    spec = next(s for s in TOOL_SPECS if s.name == "render_chart")
    assert "COLUMNS OF THAT RESULT" in spec.description
    assert "never retype the values" in spec.description
    # One measure, and the breakdown routed to `series` rather than a second y.
    assert "two MEASURES means two charts" in spec.description
    assert "a breakdown is `series`, not a second y" in spec.description
    # The kind enum is the registered set, not a prose list that could drift.
    assert set(spec.parameters["properties"]["kind"]["enum"]) == set(
        chat_tools.charts.CHART_KINDS
    )
    assert set(spec.parameters["required"]) == {"chart_ref", "kind", "title", "x", "y"}


def test_the_graph_stats_spec_ships_its_own_schema_doc() -> None:
    """The two stats tools must not share a description — the whole risk is that
    the model confuses one database for the other."""
    from whygraph.chat.graph_stats_sql import _GRAPH_SCHEMA_DOC
    from whygraph.chat.stats_sql import _SCHEMA_DOC

    spec = next(s for s in TOOL_SPECS if s.name == "run_graph_stats")
    assert spec.description == _GRAPH_SCHEMA_DOC
    assert spec.description != _SCHEMA_DOC
    assert "DIFFERENT DATABASE from run_project_stats" in spec.description


def test_a_charted_result_stays_parseable_at_the_row_cap() -> None:
    """Risk 1, measured rather than estimated.

    Length alone is not the assertion — `_encode` slices mid-array and appends a
    marker, so the defect is unparseable JSON, not size. This is the shape that
    once shipped broken on `find_changes`.
    """
    long_path = "src/whygraph/playground/src/components/chat/ToolCallCard.tsx"
    registry = ToolRegistry(max_rationale_generations=0)
    produced = registry._mint_chart_ref(
        {
            "columns": ["path", "changes"],
            "rows": [[f"{long_path}#{n}", n] for n in range(200)],
            "row_count": 200,
            "truncated": False,
        }
    )
    raw = registry.dispatch(
        "render_chart",
        {
            "chart_ref": produced["chart_ref"],
            "kind": "bar_h",
            "title": "Files changed most often",
            "x": "path",
            "y": "changes",
        },
    )
    assert not raw.endswith(TRUNCATION_MARKER)
    assert len(raw) < MAX_RESULT_CHARS
    result = json.loads(raw)  # the assertion that actually matters
    assert result["row_count"] == 200


# ---------------------------------------------------------------------------
# get_work_in_flight and get_area_owners (the Lead tools, plan 38 §5.9)
# ---------------------------------------------------------------------------

_NOW = "2026-10-28T10:00:00+00:00"


def _areas(monkeypatch: pytest.MonkeyPatch, *items) -> None:
    """Configure areas on the bound config (sorted by name, as load does)."""
    import dataclasses

    monkeypatch.setattr(
        core,
        "_config",
        dataclasses.replace(
            core._config, areas=tuple(sorted(items, key=lambda a: a.name))
        ),
    )


def _freeze(monkeypatch: pytest.MonkeyPatch, now: str = _NOW) -> None:
    from datetime import datetime

    from whygraph.mcp import resources

    monkeypatch.setattr(resources, "_utcnow", lambda: datetime.fromisoformat(now))


def _pr(number: int, **overrides):
    from whygraph.db.models import PullRequest

    fields = {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "head_sha": f"head{number}",
        "head_ref": f"feat/{number}",
        "base_ref": "main",
        "author": "dev",
        "html_url": f"https://example.invalid/pr/{number}",
        "labels": "[]",
        "fetched_at": "2026-10-25T00:00:00Z",
    }
    fields.update(overrides)
    return PullRequest(**fields)


def _issue(number: int, **overrides):
    from whygraph.db.models import Issue

    fields = {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        "author": "dev",
        "html_url": f"https://example.invalid/issue/{number}",
        "labels": "[]",
        "fetched_at": "2026-10-25T00:00:00Z",
    }
    fields.update(overrides)
    return Issue(**fields)


def _row_commit(sha: str, when: str, *, name="dev", email="dev@example.com", **kw):
    from whygraph.db.models import Commit

    fields = {
        "sha": sha,
        "parent_shas": "p0",
        "author_name": name,
        "author_email": email,
        "authored_at": when,
        "committed_at": when,
        "subject": f"subject {sha}",
        "body": "",
        "files_changed": 1,
        "insertions": 1,
        "deletions": 0,
        "scanned_at": _NOW,
        "on_default_branch": 1,
    }
    fields.update(kw)
    return Commit(**fields)


@pytest.fixture
def in_flight(chat_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Open work at a frozen clock, with two areas configured.

    * #412 is labelled into ``checkout``; its newest *human* comment is
      2026-10-20 (a bot commented later, which moved ``updated_at``).
    * #413 is an unlabelled draft whose head commit is scanned locally and
      touches ``src/billing/`` - placed by commits.
    * #414 is unlabelled with no scanned commits - no area.
    * #400 is closed and must not appear.
    """
    from alembic import command

    from whygraph.core.config import AreaConfig
    from whygraph.db import get_session
    from whygraph.db.bootstrap import alembic_config
    from whygraph.db.models import CommitFileChange, ForgeCrawlState, PRIssueLink

    command.upgrade(alembic_config(), "head")
    _areas(
        monkeypatch,
        AreaConfig("checkout", "Checkout", ("src/checkout/",), ("area:checkout",)),
        AreaConfig("billing", "Billing", ("src/billing",)),
    )
    _freeze(monkeypatch)
    comments = [
        {"author": "alice", "body": "looks good", "created_at": "2026-10-20T10:00:00Z"},
        {
            "author": "dependabot[bot]",
            "body": "rebased",
            "created_at": "2026-10-27T10:00:00Z",
        },
    ]
    with get_session() as session:
        session.add(
            _pr(
                412,
                labels='["area:checkout"]',
                created_at="2026-10-19T10:00:00Z",
                updated_at="2026-10-27T10:00:00Z",
                comments=json.dumps(comments),
            )
        )
        session.add(
            _pr(
                413,
                draft=1,
                head_sha="c_open",
                created_at="2026-10-01T10:00:00Z",
                updated_at="2026-10-10T10:00:00Z",
            )
        )
        session.add(_pr(414, updated_at="2026-10-28T08:00:00Z"))
        session.add(_pr(400, state="closed"))
        session.add(_issue(300, updated_at="2026-09-18T10:00:00Z"))
        session.add(
            _issue(301, labels='["Area:Checkout"]', updated_at="2026-10-20T10:00:00Z")
        )
        session.add(_issue(302, state="closed"))
        session.add(PRIssueLink(pr_number=412, issue_number=300, link_kind="closes"))
        session.add(PRIssueLink(pr_number=400, issue_number=301, link_kind="closes"))
        session.add(_row_commit("c_open", "2026-10-01T09:00:00Z", on_default_branch=0))
        session.add(
            CommitFileChange(
                commit_sha="c_open", path="src/billing/pay.py", change_type="M"
            )
        )
        session.add(
            ForgeCrawlState(
                kind="pull_request",
                repo="o/r",
                crawled_at="2026-10-25T08:12:00+00:00",
            )
        )
        session.add(
            ForgeCrawlState(
                kind="issue", repo="o/r", crawled_at="2026-10-27T08:12:00+00:00"
            )
        )
        session.commit()
    return chat_repo


def _wip(**kwargs) -> dict:
    return _result(
        ToolRegistry(max_rationale_generations=0), "get_work_in_flight", **kwargs
    )


def test_work_in_flight_lists_open_items_with_idle_time(in_flight: Path) -> None:
    result = _wip()

    assert result["as_of"] == "2026-10-28T10:00:00Z"
    # The older of the two crawl stamps dates both lists.
    assert result["forge_data_as_of"] == "2026-10-25T08:12:00+00:00"
    assert "3 days old" in result["freshness_warning"]
    assert result["not_tracked"] == [
        "team_review_requests",
        "deployments",
        "incidents",
    ]
    assert result["summary"] == {
        "open_prs": 3,
        "drafts": 1,
        "stale_prs": 1,
        "stale_drafts": 1,
        "open_issues": 2,
        "stale_issues": 1,
    }
    assert result["omitted"] == 0
    prs = {p["number"]: p for p in result["pull_requests"]}
    # Longest idle first; the closed PR is absent.
    assert [p["number"] for p in result["pull_requests"]] == [413, 412, 414]

    # Bot comment ignored: last activity is the human one, not updated_at.
    assert prs[412]["last_activity_at"] == "2026-10-20T10:00:00Z"
    assert prs[412]["idle_days"] == 8
    assert prs[412]["age_days"] == 9
    assert prs[412]["stale"] is True
    assert prs[412]["closes_issues"] == [300]
    assert (prs[412]["areas"], prs[412]["area_source"]) == (["checkout"], "labels")
    # A draft is never stale, however idle.
    assert prs[413]["draft"] is True and prs[413]["stale"] is False
    assert (prs[413]["areas"], prs[413]["area_source"]) == (["billing"], "commits")
    assert (prs[414]["areas"], prs[414]["area_source"]) == ([], None)
    assert prs[414]["last_activity_at"] == "2026-10-28T08:00:00Z"
    assert prs[414]["stale"] is False

    assert [i["number"] for i in result["issues"]] == [300, 301]
    issues = {i["number"]: i for i in result["issues"]}
    assert issues[300]["idle_days"] == 40 and issues[300]["stale"] is True
    assert issues[300]["linked_open_prs"] == [412]
    # Only an OPEN PR counts as linked; #400 is closed.
    assert issues[301]["linked_open_prs"] == []
    assert issues[301]["areas"] == ["checkout"]  # labels compare case-insensitively


def test_work_in_flight_filters_by_area(in_flight: Path) -> None:
    checkout = _wip(area="checkout")
    assert checkout["area"] == "checkout"
    assert [p["number"] for p in checkout["pull_requests"]] == [412]
    assert [i["number"] for i in checkout["issues"]] == [301]
    assert checkout["summary"]["open_prs"] == 1

    billing = _wip(area="Billing")
    assert [p["number"] for p in billing["pull_requests"]] == [413]
    assert billing["issues"] == []


def test_work_in_flight_unknown_area_names_the_configured_ones(in_flight: Path) -> None:
    error = _wip(area="nope")["error"]
    assert "billing" in error and "checkout" in error


def test_work_in_flight_clamps_and_counts_what_it_omits(in_flight: Path) -> None:
    result = _wip(limit=1)
    assert len(result["pull_requests"]) == 1
    assert len(result["issues"]) == 1
    assert result["omitted"] == 3
    assert "area" in result["note"] and "limit" in result["note"]
    # Summary still covers everything, not just the listed rows.
    assert result["summary"]["open_prs"] == 3

    assert len(_wip(limit=0)["pull_requests"]) == 1
    assert _wip(limit=999)["omitted"] == 0
    clamped = _wip(stale_days=0, issue_stale_days=10_000)
    assert (clamped["stale_days"], clamped["issue_stale_days"]) == (1, 365)
    # With a one-day threshold #414 (idle 0) is still fresh, #412 stale.
    assert clamped["summary"]["stale_prs"] == 1


def test_work_in_flight_long_threads_fall_back_to_updated_at(
    in_flight: Path,
) -> None:
    """A PR at the crawl's 100-comment page may have newer comments unseen."""
    from whygraph.db import get_session

    many = [
        {"author": "alice", "body": "x", "created_at": "2026-10-02T00:00:00Z"}
    ] * 100
    with get_session() as session:
        session.add(
            _pr(
                415,
                updated_at="2026-10-26T10:00:00Z",
                comments=json.dumps(many),
            )
        )
        session.commit()
    pr = next(p for p in _wip()["pull_requests"] if p["number"] == 415)
    assert pr["last_activity_at"] == "2026-10-26T10:00:00Z"


def test_work_in_flight_freshness(
    in_flight: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, "2026-10-25T20:00:00+00:00")
    assert "freshness_warning" not in _wip()

    from sqlmodel import delete

    from whygraph.db import get_session
    from whygraph.db.models import ForgeCrawlState

    with get_session() as session:
        session.exec(delete(ForgeCrawlState))
        session.commit()
    result = _wip()
    assert result["forge_data_as_of"] is None
    assert "No GitHub crawl" in result["freshness_warning"]


def test_work_in_flight_without_areas_attributes_nothing(
    in_flight: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _areas(monkeypatch)
    result = _wip()
    assert {p["area_source"] for p in result["pull_requests"]} == {None}
    assert all(p["areas"] == [] for p in result["pull_requests"])


def test_recent_activity_dates_the_forge_lists(in_flight: Path) -> None:
    result = _result(ToolRegistry(max_rationale_generations=0), "list_recent_activity")
    assert result["forge_data_as_of"] == "2026-10-25T08:12:00+00:00"


def test_recent_activity_forge_date_is_null_without_a_crawl(
    seeded_history: Path,
) -> None:
    result = _result(ToolRegistry(max_rationale_generations=0), "list_recent_activity")
    assert result["forge_data_as_of"] is None


@pytest.fixture
def owned(chat_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Commit history under ``src/checkout/`` with every exclusion represented.

    Counted: alice 3 (two addresses, one cluster), bob 2 (inactive since
    March), carol 1, and "Dave Web" 1 through the shared ``noreply`` address
    (in two clusters, so it falls back to the commit's name).
    Excluded: one merge, two bots (by address, and by resolved login), one
    refactor sweep. Ignored: out of window, off the default branch, outside
    the area.
    """
    from alembic import command

    from whygraph.core.config import AreaConfig
    from whygraph.db import get_session
    from whygraph.db.bootstrap import alembic_config
    from whygraph.db.models import Author, CommitFileChange

    command.upgrade(alembic_config(), "head")
    _areas(
        monkeypatch,
        AreaConfig("checkout", "Checkout", ("src/checkout/",), ("area:checkout",)),
    )
    _freeze(monkeypatch)

    ids = iter(range(1, 100))

    def author(login, name, emails, last_seen):
        return Author(
            id=next(ids),
            primary_login=login,
            primary_name=name,
            primary_email=emails[0],
            emails=json.dumps(emails),
            logins=json.dumps([login] if login else []),
            names=json.dumps([name]),
            last_seen=last_seen,
        )

    commits = [
        # sha, when, name, email, path, extra
        ("a1", "2026-10-01T00:00:00Z", "Alice", "alice@x", "src/checkout/a.py", {}),
        ("a2", "2026-09-20T00:00:00Z", "Alice", "alice@home", "src/checkout/b.py", {}),
        ("a3", "2026-09-10T00:00:00Z", "Alice", "alice@x", "src/checkout/a.py", {}),
        ("b1", "2026-03-01T00:00:00Z", "Bob", "bob@x", "src/checkout/a.py", {}),
        ("b2", "2026-02-01T00:00:00Z", "Bob", "bob@x", "src/checkout/a.py", {}),
        ("c1", "2026-09-01T00:00:00Z", "Carol", "carol@x", "src/checkout/c.py", {}),
        (
            "d1",
            "2026-10-05T00:00:00Z",
            "Dave Web",
            "noreply@github.com",
            "src/checkout/d.py",
            {},
        ),
        (
            "m1",
            "2026-10-02T00:00:00Z",
            "Alice",
            "alice@x",
            "src/checkout/a.py",
            {"parent_shas": "p1 p2"},
        ),
        (
            "bot1",
            "2026-10-03T00:00:00Z",
            "github-actions[bot]",
            "41898282+github-actions[bot]@users.noreply.github.com",
            "src/checkout/a.py",
            {},
        ),
        (
            "bot2",
            "2026-10-04T00:00:00Z",
            "Renovate",
            "renovate@x",
            "src/checkout/a.py",
            {},
        ),
        (
            "r1",
            "2026-02-15T00:00:00Z",
            "Bob",
            "bob@x",
            "src/checkout/a.py",
            {"refactor_score": 60},
        ),
        ("old", "2024-01-01T00:00:00Z", "Bob", "bob@x", "src/checkout/a.py", {}),
        (
            "off",
            "2026-10-06T00:00:00Z",
            "Carol",
            "carol@x",
            "src/checkout/a.py",
            {"on_default_branch": 0},
        ),
        ("out", "2026-10-07T00:00:00Z", "Carol", "carol@x", "src/checkoutx/a.py", {}),
    ]
    with get_session() as session:
        session.add(
            author(
                "alice",
                "Alice",
                ["alice@home", "alice@x", "noreply@github.com"],
                "2026-10-02T00:00:00Z",
            )
        )
        session.add(author("bob", "Bob", ["bob@x"], "2026-03-01T00:00:00Z"))
        session.add(author("carol", "Carol", ["carol@x"], "2026-10-07T00:00:00Z"))
        session.add(
            author(
                "dave", "Dave", ["dave@x", "noreply@github.com"], "2026-01-01T00:00:00Z"
            )
        )
        session.add(
            author("renovate[bot]", "Renovate", ["renovate@x"], "2026-10-04T00:00:00Z")
        )
        for sha, when, name, email, path, extra in commits:
            session.add(_row_commit(sha, when, name=name, email=email, **extra))
            session.add(CommitFileChange(commit_sha=sha, path=path, change_type="M"))
        session.commit()
    return chat_repo


def _owners(**kwargs) -> dict:
    return _result(
        ToolRegistry(max_rationale_generations=0), "get_area_owners", **kwargs
    )


def test_area_owners_counts_shares_and_excludes_noise(owned: Path) -> None:
    result = _owners(area="checkout")

    assert result["scope"] == {
        "area": "checkout",
        "paths": ["src/checkout/"],
        "since_days": 365,
    }
    assert result["excluded"] == {"merges": 1, "bots": 2, "refactors": 1}
    assert result["total_commits"] == 7
    owners = {o["name"]: o for o in result["owners"]}
    assert [o["name"] for o in result["owners"]] == [
        "Alice",
        "Bob",
        "Carol",
        "Dave Web",
    ]
    assert owners["Alice"] == {
        "name": "Alice",
        "login": "alice",
        "commits": 3,
        "share": 0.43,
        "files_touched": 2,
        "last_touched_at": "2026-10-01T00:00:00Z",
        "last_active_at": "2026-10-02T00:00:00Z",
    }
    # The shared address sits in two clusters: no login, the commit's name.
    assert owners["Dave Web"]["login"] is None
    assert owners["Dave Web"]["last_active_at"] == "2026-10-05T00:00:00Z"
    # 3 + 2 of 7 reach half; among the active (Bob is not), Alice alone does.
    assert result["contributor_absence_factor"] == 2
    assert result["active_contributor_absence_factor"] == 1
    assert "ownership, not performance" in result["note"]
    assert "omitted" not in result


def test_area_owners_by_path(owned: Path) -> None:
    result = _owners(path="./src/checkout/a.py")
    assert result["scope"] == {"path": "src/checkout/a.py", "since_days": 365}
    assert {o["name"]: o["commits"] for o in result["owners"]} == {"Alice": 2, "Bob": 2}


def test_area_owners_needs_exactly_one_of_area_or_path(owned: Path) -> None:
    assert "exactly one" in _owners()["error"]
    assert "exactly one" in _owners(area="checkout", path="src")["error"]
    assert "checkout" in _owners(area="nope")["error"]


def test_area_owners_clamps(owned: Path) -> None:
    result = _owners(area="checkout", limit=1, since_days=0)
    assert result["scope"]["since_days"] == 1
    assert result["owners"] == []
    assert result["total_commits"] == 0
    assert result["contributor_absence_factor"] == 0
    assert result["active_contributor_absence_factor"] == 0

    wide = _owners(area="checkout", limit=1, since_days=99_999)
    assert wide["scope"]["since_days"] == 3650
    assert len(wide["owners"]) == 1
    assert wide["omitted"] == 3
    # The window now reaches the 2024 commit; the factor covers every owner.
    assert wide["total_commits"] == 8
    assert wide["contributor_absence_factor"] == 2


# ---------------------------------------------------------------------------
# Phase 2: get_work_in_flight fields, compare_refs, get_milestones (plan 38)
# ---------------------------------------------------------------------------


def _milestone(number: int, **overrides):
    from whygraph.db.models import Milestone

    fields = {
        "number": number,
        "title": f"M{number}",
        "state": "open",
        "updated_at": "2026-10-01T00:00:00Z",
        "html_url": f"https://example.invalid/milestone/{number}",
        "fetched_at": "2026-10-25T00:00:00Z",
    }
    fields.update(overrides)
    return Milestone(**fields)


def test_work_in_flight_carries_the_phase_2_fields(in_flight: Path) -> None:
    from sqlmodel import select

    from whygraph.db import get_session
    from whygraph.db.models import Issue, PullRequest

    with get_session() as session:
        session.add(_milestone(3, title="v2.1"))
        pr = session.exec(select(PullRequest).where(PullRequest.number == 414)).one()
        pr.files = json.dumps(["src/checkout/cart.py", "README.md"])
        pr.review_decision = "CHANGES_REQUESTED"
        pr.review_requests = json.dumps(["bob"])
        pr.first_review_at = "2026-10-27T09:00:00Z"
        pr.assignees = json.dumps(["alice"])
        pr.milestone = 3
        pr.additions, pr.deletions, pr.changed_files = 120, 4, 2
        session.add(pr)
        labelled = session.exec(
            select(PullRequest).where(PullRequest.number == 412)
        ).one()
        labelled.files = json.dumps(["src/billing/pay.py"])  # labels still win
        session.add(labelled)
        issue = session.exec(select(Issue).where(Issue.number == 300)).one()
        issue.assignees = json.dumps(["carol"])
        issue.milestone = 9  # not in the milestone table: title unknown
        session.add(issue)
        session.commit()

    result = _wip()
    prs = {p["number"]: p for p in result["pull_requests"]}
    assert (prs[414]["areas"], prs[414]["area_source"]) == (["checkout"], "files")
    assert prs[414]["review_decision"] == "CHANGES_REQUESTED"
    assert prs[414]["review_requests"] == ["bob"]
    assert prs[414]["first_review_at"] == "2026-10-27T09:00:00Z"
    assert prs[414]["assignees"] == ["alice"]
    assert prs[414]["milestone"] == {"number": 3, "title": "v2.1"}
    assert (prs[414]["additions"], prs[414]["changed_files"]) == (120, 2)
    assert (prs[412]["areas"], prs[412]["area_source"]) == (["checkout"], "labels")
    # No labels and no files: still placed by its scanned commits.
    assert (prs[413]["areas"], prs[413]["area_source"]) == (["billing"], "commits")
    assert prs[413]["milestone"] is None and prs[413]["review_requests"] == []

    issues = {i["number"]: i for i in result["issues"]}
    assert issues[300]["assignees"] == ["carol"]
    assert issues[300]["milestone"] == {"number": 9, "title": None}

    assert [p["number"] for p in _wip(area="checkout")["pull_requests"]] == [
        412,
        414,
    ]


def test_work_in_flight_files_that_map_nowhere_still_decide(in_flight: Path) -> None:
    """A PR whose files touch no area has none, rather than falling to commits."""
    from sqlmodel import select

    from whygraph.db import get_session
    from whygraph.db.models import PullRequest

    with get_session() as session:
        pr = session.exec(select(PullRequest).where(PullRequest.number == 413)).one()
        pr.files = json.dumps(["docs/index.md"])
        session.add(pr)
        session.commit()
    prs = {p["number"]: p for p in _wip()["pull_requests"]}
    assert (prs[413]["areas"], prs[413]["area_source"]) == ([], "files")


def _git(root: Path, *args: str, when: str | None = None) -> str:
    import os
    import subprocess

    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(
        ["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def released(chat_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """A real four-commit repository with two tags, scanned into the DB.

    * ``c1`` (2026-03-01, ``src/checkout/a.py``) is tagged ``v1.0``;
    * ``c2`` (2026-03-02, ``src/billing/b.py``) is #50's merge commit;
    * ``c3`` (2026-03-03, ``src/checkout/c.py``) is in #51's commit list and
      tagged ``v1.1``; #51 is labelled ``area:checkout`` and closes #20;
    * ``c4`` (2026-03-04, ``docs/notes.md``) is in no area and no PR.
    """
    import shutil

    from alembic import command

    from whygraph.core.config import AreaConfig
    from whygraph.db import get_session
    from whygraph.db.bootstrap import alembic_config
    from whygraph.db.models import CommitFileChange, PRIssueLink, Tag

    shutil.rmtree(chat_repo / ".git")
    _git(chat_repo, "init", "-q", "-b", "main")
    files = ["src/checkout/a.py", "src/billing/b.py", "src/checkout/c.py"]
    files.append("docs/notes.md")
    shas: list[str] = []
    for n, path in enumerate(files, start=1):
        target = chat_repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{n}\n", encoding="utf-8")
        when = f"2026-03-0{n}T12:00:00+00:00"
        _git(chat_repo, "add", path)
        _git(chat_repo, "commit", "-q", "-m", f"commit {n}", when=when)
        shas.append(_git(chat_repo, "rev-parse", "HEAD"))

    command.upgrade(alembic_config(), "head")
    _areas(
        monkeypatch,
        AreaConfig("checkout", "Checkout", ("src/checkout/",), ("area:checkout",)),
        AreaConfig("billing", "Billing", ("src/billing",)),
    )
    with get_session() as session:
        for n, (sha, path) in enumerate(zip(shas, files), start=1):
            session.add(
                _row_commit(sha, f"2026-03-0{n}T12:00:00+00:00", subject=f"commit {n}")
            )
            session.add(CommitFileChange(commit_sha=sha, path=path, change_type="A"))
        session.add(Tag(name="v1.0", sha=shas[0], tagged_at="2026-03-01T12:00:00Z"))
        session.add(Tag(name="v1.1", sha=shas[2], tagged_at="2026-03-03T12:00:00Z"))
        session.add(
            _pr(
                50,
                state="merged",
                merge_commit_sha=shas[1],
                merged_at="2026-03-02T12:00:00Z",
            )
        )
        session.add(
            _pr(
                51,
                state="merged",
                labels='["area:checkout"]',
                commit_titles=json.dumps([{"oid": shas[2], "title": "commit 3"}]),
                merged_at="2026-03-03T13:00:00Z",
            )
        )
        session.add(_issue(20, state="closed", closed_at="2026-03-03T13:00:00Z"))
        session.add(PRIssueLink(pr_number=51, issue_number=20, link_kind="closes"))
        session.commit()
    return SimpleNamespace(root=chat_repo, shas=shas)


def _compare(**kwargs) -> dict:
    return _result(ToolRegistry(max_rationale_generations=0), "compare_refs", **kwargs)


def test_compare_refs_between_two_tags_groups_by_area(released) -> None:
    c1, c2, c3, _ = released.shas
    result = _compare(from_ref="v1.0", to_ref="v1.1")

    assert result["from"] == {
        "ref": "v1.0",
        "kind": "tag",
        "sha": c1,
        "date": "2026-03-01T12:00:00Z",
    }
    assert (result["to"]["kind"], result["to"]["sha"]) == ("tag", c3)
    assert result["total_commits"] == 2 and result["not_scanned"] == 0
    assert [c["sha"] for c in result["commits"]] == [c3, c2]
    commits = {c["sha"]: c for c in result["commits"]}
    assert commits[c3]["areas"] == ["checkout"]
    assert commits[c3]["pull_requests"] == [51]
    assert commits[c2]["pull_requests"] == [50]

    prs = {p["number"]: p for p in result["pull_requests"]}
    assert [p["number"] for p in result["pull_requests"]] == [51, 50]
    assert (prs[51]["areas"], prs[51]["area_source"]) == (["checkout"], "labels")
    assert (prs[50]["areas"], prs[50]["area_source"]) == (["billing"], "commits")
    assert prs[51]["closes_issues"] == [20]
    assert [i["number"] for i in result["issues_closed"]] == [20]

    assert result["by_area"] == [
        {
            "area": "billing",
            "commits": 1,
            "pull_request_count": 1,
            "pull_requests": [50],
        },
        {
            "area": "checkout",
            "commits": 1,
            "pull_request_count": 1,
            "pull_requests": [51],
        },
    ]
    assert result["omitted"] == 0 and "note" not in result


def test_compare_refs_resolves_sha_prefixes_dates_and_the_latest(released) -> None:
    c1, c2, c3, c4 = released.shas
    by_sha = _compare(from_ref=c1[:8].upper(), to_ref="2026-03-03")
    assert (by_sha["from"]["kind"], by_sha["from"]["sha"]) == ("sha", c1)
    assert (by_sha["to"]["kind"], by_sha["to"]["sha"]) == ("date", c3)
    assert [c["sha"] for c in by_sha["commits"]] == [c3, c2]

    # A datetime is a point in time: c3 at noon is after 06:00.
    morning = _compare(from_ref="v1.0", to_ref="2026-03-03T06:00:00Z")
    assert morning["to"]["sha"] == c2

    latest = _compare(from_ref="v1.1")
    assert (latest["to"]["kind"], latest["to"]["sha"]) == ("latest", c4)
    assert [c["sha"] for c in latest["commits"]] == [c4]
    assert latest["by_area"] == [
        {"area": None, "commits": 1, "pull_request_count": 0, "pull_requests": []}
    ]


def test_compare_refs_only_full_db_shas_reach_git_argv(
    released, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.services.git import repository

    argvs: list[list[str]] = []
    real = repository.GitRevListShasCmd

    class Recording(real):
        def argv(self) -> list[str]:
            argv = super().argv()
            argvs.append(argv)
            return argv

    monkeypatch.setattr(repository, "GitRevListShasCmd", Recording)
    c1, _, c3, _ = released.shas
    _compare(from_ref="v1.0", to_ref=c3[:10])
    assert argvs == [["git", "rev-list", "--end-of-options", c3, f"^{c1}"]]

    argvs.clear()
    for hostile in ("--output=/tmp/x", "HEAD~1", "main", "v1.0..v1.1", "^v1.0"):
        result = _compare(from_ref=hostile, to_ref="v1.1")
        assert "is not a known tag" in result["error"], hostile
    assert argvs == []


def test_range_shas_refuses_anything_but_full_shas(released) -> None:
    from whygraph.services.git import GitError, Repository

    c1, _, c3, _ = released.shas
    repo = Repository(released.root)
    assert repo.range_shas(c1, c3) == frozenset(released.shas[1:3])
    for bad in (c1[:12], "--all", f"{c1}\n", "HEAD"):
        with pytest.raises(GitError):
            repo.range_shas(bad, c3)


def test_compare_refs_errors_are_results(released) -> None:
    from whygraph.db import get_session

    unknown = _compare(from_ref="v9")["error"]
    assert "v1.1, v1.0" in unknown  # the newest tags, newest first
    assert "required" in _compare(from_ref="  ")["error"]
    assert "on or before" in _compare(from_ref="2020-01-01")["error"]

    with get_session() as session:
        session.add(_row_commit("abcdef1" + "0" * 33, "2026-01-01T00:00:00Z"))
        session.add(_row_commit("abcdef1" + "1" * 33, "2026-01-01T00:00:00Z"))
        session.commit()
    assert "more than one commit" in _compare(from_ref="abcdef1")["error"]


def test_compare_refs_swapped_refs_say_so(released) -> None:
    result = _compare(from_ref="v1.1", to_ref="v1.0")
    assert result["total_commits"] == 0
    assert "swap" in result["note"]


def test_compare_refs_batches_the_pr_lookup_and_clamps(
    released, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.mcp import resources

    def _per_commit(*_args, **_kwargs):
        raise AssertionError("compare_refs must not look PRs up one commit at a time")

    monkeypatch.setattr(resources, "_linked_prs", _per_commit)
    result = _compare(from_ref="v1.0", limit=1)
    assert len(result["commits"]) == 1 and len(result["pull_requests"]) == 1
    assert result["omitted"] == 2 + 1  # commits c3, c2 and PR #50
    assert result["total_commits"] == 3
    assert "limit 1" in result["note"]
    assert _compare(from_ref="v1.0", limit=999)["limit"] == 50


def test_compare_refs_without_areas_groups_nothing(
    released, monkeypatch: pytest.MonkeyPatch
) -> None:
    _areas(monkeypatch)
    result = _compare(from_ref="v1.0", to_ref="v1.1")
    assert result["by_area"] == []
    assert all(c["areas"] == [] for c in result["commits"])
    assert "No areas are configured" in result["note"]


@pytest.fixture
def milestones(chat_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Four milestones at a frozen clock (2026-10-28), #2 with real items.

    #2 (due 2026-11-15) holds issues opened 10-01 (closed 10-10), 10-05 and
    10-20 (both open), and PR #60 opened 10-02 and merged 10-12.
    """
    from alembic import command

    from whygraph.db import get_session
    from whygraph.db.bootstrap import alembic_config

    command.upgrade(alembic_config(), "head")
    _freeze(monkeypatch)
    with get_session() as session:
        session.add(
            _milestone(1, title="v1", state="closed", closed_at="2026-09-30T00:00:00Z")
        )
        session.add(
            _milestone(
                2,
                title="v2",
                due_on="2026-11-15T00:00:00Z",
                progress_pct=50.0,
                description="x" * 500,
            )
        )
        session.add(_milestone(3, title="v3"))
        session.add(_milestone(4, title="v2.5", due_on="2026-10-20T00:00:00Z"))
        session.add(
            _issue(
                70,
                milestone=2,
                state="closed",
                created_at="2026-10-01T09:00:00Z",
                closed_at="2026-10-10T09:00:00Z",
            )
        )
        session.add(_issue(71, milestone=2, created_at="2026-10-05T09:00:00Z"))
        session.add(_issue(72, milestone=2, created_at="2026-10-20T09:00:00Z"))
        session.add(
            _pr(
                60,
                milestone=2,
                state="merged",
                created_at="2026-10-02T09:00:00Z",
                merged_at="2026-10-12T09:00:00Z",
                closed_at="2026-10-12T09:00:00Z",
            )
        )
        session.commit()
    return chat_repo


def test_milestones_list_open_by_due_date_with_counts(milestones: Path) -> None:
    result = _result(ToolRegistry(max_rationale_generations=0), "get_milestones")

    assert result["state"] == "open"
    assert [m["number"] for m in result["milestones"]] == [4, 2, 3]
    listed = {m["number"]: m for m in result["milestones"]}
    assert (listed[4]["days_to_due"], listed[4]["overdue"]) == (-8, True)
    assert (listed[2]["days_to_due"], listed[2]["overdue"]) == (18, False)
    assert (listed[3]["days_to_due"], listed[3]["overdue"]) == (None, False)
    assert listed[2]["issues"] == {"open": 2, "closed": 1}
    assert listed[2]["pull_requests"] == {"open": 0, "merged": 1, "closed": 0}
    assert listed[2]["progress_pct"] == 50.0
    assert len(listed[2]["description"]) == 241
    # The first listed (#4) has no items: nothing to chart, so no ref.
    assert result["burndown"]["number"] == 4
    assert result["rows"] == [] and "chart_ref" not in result
    assert result["forge_data_as_of"] is None
    assert "No GitHub crawl" in result["freshness_warning"]

    closed = _result(
        ToolRegistry(max_rationale_generations=0), "get_milestones", state="closed"
    )
    assert [m["number"] for m in closed["milestones"]] == [1]


def test_milestone_burndown_is_chartable(milestones: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    result = _result(registry, "get_milestones", number=2)

    assert result["burndown"]["number"] == 2
    assert result["burndown"]["start"] == "2026-10-01"
    assert result["burndown"]["step_days"] == 1
    assert result["columns"] == ["date", "open", "closed"]
    rows = {row[0]: row[1:] for row in result["rows"]}
    assert len(result["rows"]) == 28
    assert rows["2026-10-01"] == [1, 0]
    assert rows["2026-10-12"] == [1, 2]  # 70 and #60 closed, 71 open
    assert rows["2026-10-28"] == [2, 2]

    chart = _result(
        registry,
        "render_chart",
        chart_ref=result["chart_ref"],
        kind="line",
        title="v2 burndown",
        x="date",
        y="open",
    )
    assert "error" not in chart
    assert chart["chart"]["kind"] == "line"
    assert chart["row_count"] == 28


def test_milestones_bad_arguments_are_results(milestones: Path) -> None:
    registry = ToolRegistry(max_rationale_generations=0)
    assert (
        "state must be one of"
        in _result(registry, "get_milestones", state="x")["error"]
    )
    error = _result(registry, "get_milestones", number=99)["error"]
    assert "#99" in error and "#2" in error
    clamped = _result(registry, "get_milestones", state="all", limit=1)
    assert len(clamped["milestones"]) == 1 and clamped["omitted"] == 3


def test_milestones_empty_table_says_why(chat_repo: Path) -> None:
    from alembic import command

    from whygraph.db.bootstrap import alembic_config

    command.upgrade(alembic_config(), "head")
    result = _result(ToolRegistry(max_rationale_generations=0), "get_milestones")
    assert result["milestones"] == [] and result["burndown"] is None
    assert "No milestones are recorded" in result["note"]


def test_a_long_burndown_is_thinned_but_ends_on_its_last_day() -> None:
    from datetime import datetime, timezone

    from whygraph.mcp.resources import _BURNDOWN_POINTS, _burndown_rows

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 20, tzinfo=timezone.utc)
    rows, step, first = _burndown_rows([(start, None)], end)
    assert first == "2026-01-01" and step > 1
    assert len(rows) <= _BURNDOWN_POINTS + 2
    assert rows[-1] == ["2026-07-20", 1, 0]


# ---------------------------------------------------------------------------
# Team notes: find_notes, propose_note, notes on cards (plan 40 step 6)
# ---------------------------------------------------------------------------

from test_mcp_notes import (  # noqa: E402
    A_SOURCE,
    PROJECT,
    _confirmed,
    _index,
    _node as _note_node,
    _propose,
    _set,
    project as _notes_project,  # noqa: F401 -- the fixture, under a private name
)
from whygraph.chat.harness import build_system_prompt  # noqa: E402
from whygraph.core.context import use_actor  # noqa: E402
from whygraph.db import get_session  # noqa: E402
from whygraph.db.models import ProjectNote  # noqa: E402
from sqlmodel import select  # noqa: E402

_SECRET = "Never call billing synchronously from checkout."


@pytest.fixture
def note_project(_notes_project):  # noqa: F811
    return _notes_project


@pytest.fixture
def notes_registry(note_project):
    with use_actor("u1"):
        yield ToolRegistry(note_origin="chat:7", max_rationale_generations=1)


def test_find_notes_is_concurrent_and_propose_note_is_serial() -> None:
    assert "propose_note" in chat_tools.SERIAL_TOOLS
    assert "find_notes" not in chat_tools.SERIAL_TOOLS


def test_find_notes_each_argument_maps_to_its_function(
    note_project,
    notes_registry,
) -> None:
    sym = _confirmed({"kind": "symbol", "qualified_name": "pkg.a"}, "Symbol fact.")
    path = _confirmed({"kind": "path", "path": "src/pkg"}, "Path fact.")
    area = _confirmed({"kind": "area", "area": "chat"}, "Area fact.")
    wide = _confirmed(PROJECT, _SECRET)
    pending = _propose("Still waiting for review.")["id"]

    def ids(**args) -> list[int]:
        return [n["id"] for n in _result(notes_registry, "find_notes", **args)["notes"]]

    assert set(ids()) == {sym, path, area, wide}  # confirmed only
    assert pending not in ids()
    assert ids(project_wide=True) == [wide]
    assert set(ids(qualified_name="pkg.a")) == {sym, path}
    assert ids(path="src/pkg") == [path] or sym in ids(path="src/pkg")
    assert area in ids(area="chat")
    assert ids(query="Symbol fact") == [sym]
    assert pending not in ids(query="waiting")


def test_find_notes_drops_whole_notes_to_fit_and_reports_more(
    note_project,
    notes_registry,
) -> None:
    for n in range(10):
        _confirmed(PROJECT, f"{n} " + "word " * 195)
    result = _result(notes_registry, "find_notes")
    text = json.dumps({"notes": result["notes"]}, default=str)
    assert len(text) <= chat_tools.FIND_NOTES_MAX_CHARS
    assert result["more"] == 10 - len(result["notes"]) > 0
    assert all(
        n["body"].endswith("word") or n["body"].endswith("word ")
        for n in result["notes"]
    )


def test_propose_note_happy_path_and_a_proposal_is_not_found(
    note_project,
    notes_registry,
) -> None:
    result = _result(
        notes_registry,
        "propose_note",
        body=_SECRET,
        kind="constraint",
        anchor={"kind": "symbol", "qualified_name": "pkg.a"},
    )
    assert list(result)[0] == "note_id"
    assert result["status"] == "proposed"
    assert result["duplicate"] is False
    assert result["review"] == f"/p/demo/notes?id={result['note_id']}"
    assert result["note"]["body"] == _SECRET
    with get_session() as session:
        row = session.exec(select(ProjectNote)).one()
        stored = (row.source, row.origin, row.proposed_by)
    assert stored == ("chat", "chat:7", "u1")
    assert _result(notes_registry, "find_notes", qualified_name="pkg.a")["notes"] == []
    assert _result(notes_registry, "find_notes", project_wide=True)["notes"] == []


def test_propose_note_dedupe_hit_on_anothers_proposal_hides_the_note(
    note_project,
    notes_registry,
) -> None:
    other = _propose(_SECRET, actor="u2")["id"]
    result = _result(
        notes_registry, "propose_note", body=_SECRET, kind="decision", anchor=PROJECT
    )
    assert result["note_id"] == other
    assert result["duplicate"] is True
    assert "note" not in result


def test_propose_note_budget_is_two_per_turn_and_not_an_error(
    note_project,
    notes_registry,
) -> None:
    for n in range(2):
        out = _result(
            notes_registry,
            "propose_note",
            body=f"Distinct fact {n}.",
            kind="decision",
            anchor=PROJECT,
        )
        assert out["status"] == "proposed"
    text, outcome = notes_registry.dispatch_outcome(
        "propose_note",
        {"body": "A third.", "kind": "decision", "anchor": PROJECT},
    )
    assert json.loads(text) == {
        "status": "not_proposed",
        "note": "the proposal budget for this turn is used",
    }
    assert outcome == "ok"
    with get_session() as session:
        assert len(session.exec(select(ProjectNote)).all()) == 2


def test_propose_note_validation_errors_come_back_as_error_results(
    note_project,
    notes_registry,
) -> None:
    result = _result(
        notes_registry, "propose_note", body="", kind="decision", anchor=PROJECT
    )
    assert "error" in result
    assert notes_registry.proposals_used == 0


def test_get_rationale_carries_notes_on_every_return(
    note_project,
    notes_registry,
    monkeypatch,
) -> None:
    note_id = _confirmed({"kind": "symbol", "qualified_name": "pkg.a"}, _SECRET)
    present = [_fake_evidence_item()]
    monkeypatch.setattr(chat_tools, "lookup_cached", lambda *a, **k: None)
    monkeypatch.setattr(
        chat_tools, "whygraph_rationale_brief", lambda **kw: {"purpose": "p"}
    )

    def get() -> dict:
        return _result(notes_registry, "get_rationale", qualified_name="pkg.a")

    monkeypatch.setattr(chat_tools, "collect_evidence", lambda t, limit=20: [])
    no_evidence = get()
    monkeypatch.setattr(chat_tools, "collect_evidence", lambda t, limit=20: present)
    generated = get()  # budget of one: generates
    not_generated = get()
    monkeypatch.setattr(
        chat_tools,
        "lookup_cached",
        lambda *a, **k: (SimpleNamespace(), "2026-01-01T00:00:00+00:00"),
    )
    monkeypatch.setattr(
        chat_tools, "_format_response", lambda *a, **k: {"purpose": "cached"}
    )
    cached = get()
    assert no_evidence["status"] == "no_evidence"
    assert generated["generated"] is True
    assert not_generated["status"] == "not_generated"
    assert cached["generated"] is False
    for result in (no_evidence, generated, not_generated, cached):
        assert [n["id"] for n in result["notes"]] == [note_id]
        assert result["notes"][0]["anchor_state"] == "current"


def test_get_rationale_shows_a_changed_note_after_the_code_is_edited(
    note_project,
    notes_registry,
    monkeypatch,
) -> None:
    _confirmed({"kind": "symbol", "qualified_name": "pkg.a"}, _SECRET)
    (note_project.root / "src/pkg/a.py").write_text(
        A_SOURCE.replace("return 1", "return 99")
    )
    _index(note_project.root, [_note_node("pkg.a", "src/pkg/a.py", 1, 2)])
    monkeypatch.setattr(chat_tools, "collect_evidence", lambda t, limit=20: [])
    result = _result(notes_registry, "get_rationale", qualified_name="pkg.a")
    assert result["notes"][0]["anchor_state"] == "changed"


def test_overview_counts_project_wide_notes_and_never_shows_a_body(
    note_project,
    notes_registry,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        chat_tools, "_repo_overview_resource", lambda: {"repository": "demo"}
    )
    assert "team_notes" not in _result(notes_registry, "get_repo_overview")
    _confirmed(PROJECT, _SECRET)
    _confirmed(PROJECT, "Another project-wide fact.")
    _confirmed({"kind": "path", "path": "src/pkg"}, "Not project wide.")
    overview = _result(notes_registry, "get_repo_overview")
    assert overview["team_notes"] == {
        "project_wide": 2,
        "hint": "call find_notes(project_wide=true)",
    }
    for message in build_system_prompt(notes_registry):
        assert _SECRET not in message.content
        assert "Another project-wide fact" not in message.content
    assert "project_wide" in build_system_prompt(notes_registry)[1].content


# ---------------------------------------------------------------------------
# Plan 41 step 6: the confirmed purpose in the overview
# ---------------------------------------------------------------------------

_PURPOSE = (
    "What it is: A booking tool.\nWho it is for: Reception.\n"
    "Main parts: api, worker.\nStack: Python"
)


def _stub_overview(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_tools, "_repo_overview_resource", lambda: {"repository": "demo"}
    )


def _confirmed_purpose(body: str = _PURPOSE) -> int:
    from whygraph.mcp import notes as mcp_notes

    note, _ = mcp_notes.propose_purpose(body, actor_uid="u1", run_id=None)
    mcp_notes.confirm(note["id"], actor_uid="u2", actor_can_configure=True)
    return note["id"]


def test_overview_carries_only_a_confirmed_purpose(
    note_project,
    notes_registry,
    monkeypatch,
) -> None:
    from whygraph.mcp import notes as mcp_notes

    _stub_overview(monkeypatch)
    assert "purpose" not in _result(notes_registry, "get_repo_overview")
    note, _ = mcp_notes.propose_purpose(_PURPOSE, actor_uid="u1", run_id=None)
    assert "purpose" not in _result(notes_registry, "get_repo_overview")  # proposed
    mcp_notes.confirm(note["id"], actor_uid="u2", actor_can_configure=True)
    purpose = _result(notes_registry, "get_repo_overview")["purpose"]
    assert purpose["source"] == "analysis"
    assert purpose["text"].startswith(
        f"- note #{note['id']} [purpose] (project, n/a): "
    )
    assert "A booking tool." in purpose["text"] and "\n" not in purpose["text"]
    mcp_notes.retire(note["id"], actor_uid="u2", reason="wrong")
    assert "purpose" not in _result(notes_registry, "get_repo_overview")  # retired


def test_the_purpose_is_not_counted_as_a_team_note(
    note_project,
    notes_registry,
    monkeypatch,
) -> None:
    _stub_overview(monkeypatch)
    _confirmed_purpose()
    overview = _result(notes_registry, "get_repo_overview")
    assert "purpose" in overview and "team_notes" not in overview
    _confirmed(PROJECT, "A project-wide fact.")
    assert _result(notes_registry, "get_repo_overview")["team_notes"] == {
        "project_wide": 1,
        "hint": "call find_notes(project_wide=true)",
    }


def test_a_long_purpose_is_cut_at_a_space_within_600_with_the_prefix(
    note_project,
    notes_registry,
    monkeypatch,
) -> None:
    _stub_overview(monkeypatch)
    note_id = _confirmed_purpose()
    _set(note_id, body="word " * 300)
    text = _result(notes_registry, "get_repo_overview")["purpose"]["text"]
    assert text.startswith(f"- note #{note_id} [purpose] (project, n/a): ")
    assert len(text) <= 600 and text.endswith("...")
    assert not text[:-3].endswith(" ")


def test_a_long_non_ascii_purpose_is_cut_cleanly_never_past_600(
    note_project,
    notes_registry,
    monkeypatch,
) -> None:
    _stub_overview(monkeypatch)
    note_id = _confirmed_purpose()
    # 400 characters, but json.dumps escapes each one to a 6-character \uXXXX.
    _set(note_id, body=" ".join(["\u65e5\u672c\u8a9e"] * 100))
    text = _result(notes_registry, "get_repo_overview")["purpose"]["text"]
    assert len(text) <= 600 and text.endswith("...")
    body = text[len("- note #") :]
    assert "\\u65e5" in body and not body[:-3].endswith("\\")
    assert not text[:-3].endswith(" ")


def test_a_linked_project_skips_the_purpose_lookup(
    note_project,
    notes_registry,
    monkeypatch,
) -> None:
    _stub_overview(monkeypatch)
    _confirmed_purpose()
    monkeypatch.setattr(chat_tools, "_is_linked", lambda: True)

    def _boom():
        raise AssertionError("purpose_notes must not run on a linked project")

    monkeypatch.setattr(chat_tools, "purpose_notes", _boom)
    overview = _result(notes_registry, "get_repo_overview")
    assert "purpose" not in overview and "team_notes" not in overview
