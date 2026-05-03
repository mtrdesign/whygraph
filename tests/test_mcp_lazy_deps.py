from __future__ import annotations

from pathlib import Path

import pytest

from whygraph import mcp_server
from whygraph.mcp_server import (
    NoCodeGraphError,
    _get_deps,
    evidence_for,
    rationale_pre_edit_brief,
)


def _node_dict(file_path: str = "src/a.py", qname: str = "pkg.a", node_id: str = "n_a") -> dict:
    return {
        "id": node_id,
        "kind": "function",
        "name": qname.rsplit(".", 1)[-1],
        "qualified_name": qname,
        "file_path": file_path,
        "language": "python",
        "start_line": 1,
        "end_line": 3,
        "docstring": None,
        "signature": None,
    }


def test_server_module_imports_with_no_codegraph_db_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The import at the top of this file already proved the module loads
    # without a CodeGraph DB present. This test just nails down the server
    # name invariant from inside an empty directory.
    monkeypatch.delenv("CODEGRAPH_DB", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert mcp_server.mcp.name == "whygraph"


def test_get_deps_raises_no_codegraph_when_db_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEGRAPH_DB", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NoCodeGraphError, match=".codegraph/codegraph.db"):
        _get_deps()


def test_no_codegraph_error_is_value_error_subclass() -> None:
    # Existing tests assert pytest.raises(ValueError, match="No CodeGraph DB").
    # Subclassing ValueError keeps that contract.
    assert issubclass(NoCodeGraphError, ValueError)


def test_evidence_for_returns_clear_error_when_codegraph_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEGRAPH_DB", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NoCodeGraphError, match=".codegraph/codegraph.db"):
        evidence_for(target="anything")


def test_rationale_returns_clear_error_when_codegraph_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEGRAPH_DB", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NoCodeGraphError, match=".codegraph/codegraph.db"):
        rationale_pre_edit_brief(target="anything")


def test_deps_are_cached_across_tool_calls(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    repo = init_git_repo()
    git_commit(repo, "src/a.py", "l1\nl2\nl3\n", message="init")
    cg_path = codegraph_db_factory(nodes=[_node_dict()], edges=[])
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_path))
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(repo)

    real_init = mcp_server.SqliteCodegraphBackend.__init__
    calls = {"n": 0}

    def counting_init(self, path):
        calls["n"] += 1
        real_init(self, path)

    monkeypatch.setattr(
        mcp_server.SqliteCodegraphBackend, "__init__", counting_init
    )

    evidence_for(target="pkg.a", response_format="json")
    evidence_for(target="pkg.a", response_format="json")
    assert calls["n"] == 1


def test_reset_deps_closes_and_clears(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    repo = init_git_repo()
    git_commit(repo, "src/a.py", "l1\nl2\nl3\n", message="init")
    cg_path = codegraph_db_factory(nodes=[_node_dict()], edges=[])
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_path))
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(repo)

    deps = _get_deps()
    assert mcp_server._DEPS is deps

    closed: list[bool] = []
    real_close = deps.backend.close

    def tracked_close():
        closed.append(True)
        real_close()

    deps.backend.close = tracked_close  # type: ignore[method-assign]
    mcp_server._reset_deps()
    assert mcp_server._DEPS is None
    assert closed == [True]


def test_atexit_handler_is_safe_when_no_deps_cached() -> None:
    mcp_server._reset_deps()
    assert mcp_server._DEPS is None
    # Should not raise.
    mcp_server._atexit_close()
