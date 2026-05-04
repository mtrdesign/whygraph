from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.mcp_server import (
    _resolve_symbol,
    evidence_for,
    format_evidence_markdown,
    mcp,
)


def _node_dict(file_path: str, qname: str = "pkg.a", node_id: str = "n_a") -> dict:
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


def _setup(
    init_git_repo,
    git_commit,
    codegraph_db_factory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    file_content: str = "l1\nl2\nl3\n",
    commit_message: str = "init",
) -> tuple[Path, str]:
    repo = init_git_repo()
    sha = git_commit(repo, "src/a.py", file_content, message=commit_message)
    cg_path = codegraph_db_factory(nodes=[_node_dict("src/a.py")], edges=[])
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(repo)
    return repo, sha


def test_first_call_collects_real_git_evidence(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    result = evidence_for(target="pkg.a", response_format="json")
    assert isinstance(result, dict)
    assert result["source"] == "collected"
    assert result["qualified_name"] == "pkg.a"
    sources = {e["source"] for e in result["evidence"]}
    assert "git_blame" in sources
    assert "git_commit" in sources


def test_second_call_returns_cache_with_same_bundle_hash(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    first = evidence_for(target="pkg.a", response_format="json")
    second = evidence_for(target="pkg.a", response_format="json")
    assert first["source"] == "collected"
    assert second["source"] == "cache"
    assert first["bundle_hash"] == second["bundle_hash"]


def test_refresh_forces_recollect(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    evidence_for(target="pkg.a", response_format="json")
    refreshed = evidence_for(target="pkg.a", refresh=True, response_format="json")
    assert refreshed["source"] == "collected"


def test_resolve_by_node_id(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    result = evidence_for(target="n_a", response_format="json")
    assert result["qualified_name"] == "pkg.a"
    assert result["node_id"] == "n_a"


def test_unknown_symbol_raises(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    with pytest.raises(ValueError, match="Symbol not found"):
        evidence_for(target="pkg.nope")


def test_markdown_response_format(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    text = evidence_for(target="pkg.a")  # default markdown
    assert isinstance(text, str)
    assert text.startswith("# Evidence: `pkg.a`")
    assert "src/a.py:1-3" in text
    assert "git_blame" in text


def test_markdown_includes_bundle_hash_prefix(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    json_result = evidence_for(target="pkg.a", response_format="json")
    text = evidence_for(target="pkg.a")
    assert json_result["bundle_hash"][:12] in text


def test_recollect_on_new_commit(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    repo, _ = _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    first = evidence_for(target="pkg.a", response_format="json")
    git_commit(repo, "src/a.py", "l1\nl2\nl3\nl4\n", message="extend")
    second = evidence_for(target="pkg.a", response_format="json")
    assert first["source"] == "collected"
    assert second["source"] == "collected"  # HEAD-sha changed → cache invalidated
    assert first["head_at_collection"] != second["head_at_collection"]


def test_evidence_for_returns_clear_error_when_no_codegraph(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("CODEGRAPH_DB", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="No CodeGraph DB"):
        evidence_for(target="anything")


def test_evidence_for_tool_is_registered() -> None:
    import anyio

    tools = anyio.run(mcp.list_tools)
    assert "whygraph_evidence_for" in {t.name for t in tools}


def test_resolve_symbol_helper_unchanged(fake_codegraph_db) -> None:
    from whygraph.backend import SqliteCodegraphBackend

    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        assert _resolve_symbol(backend, "pkg.b").id == "n_b"
        assert _resolve_symbol(backend, "n_a").qualified_name == "pkg.a"
        assert _resolve_symbol(backend, "missing") is None
    finally:
        backend.close()


def test_format_evidence_markdown_empty_evidence_block(tmp_path: Path) -> None:
    from whygraph.backend import SymbolNode
    from whygraph.evidence.types import CollectionResult

    node = SymbolNode(
        id="n_x",
        kind="function",
        name="x",
        qualified_name="pkg.x",
        file_path="src/x.py",
        language="python",
        start_line=10,
        end_line=20,
        docstring=None,
        signature=None,
    )
    collection = CollectionResult(
        evidence=[],
        bundle_hash="0" * 64,
        source="collected",
        collected_at=0,
        head_at_collection=None,
    )
    text = format_evidence_markdown(node, collection)
    assert "_(no evidence)_" in text
    assert "(none)" in text  # head_at_collection
