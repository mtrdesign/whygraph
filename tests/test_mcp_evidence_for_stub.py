from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.backend import SqliteCodegraphBackend
from whygraph.mcp_server import _resolve_symbol, evidence_for, mcp


def test_resolve_symbol_by_qualified_name(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        node = _resolve_symbol(backend, "pkg.a")
        assert node is not None
        assert node.id == "n_a"
    finally:
        backend.close()


def test_resolve_symbol_by_node_id(fake_codegraph_db: Path) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        node = _resolve_symbol(backend, "n_b")
        assert node is not None
        assert node.qualified_name == "pkg.b"
    finally:
        backend.close()


def test_resolve_symbol_returns_none_for_unknown(
    fake_codegraph_db: Path,
) -> None:
    backend = SqliteCodegraphBackend(fake_codegraph_db)
    try:
        assert _resolve_symbol(backend, "nope") is None
    finally:
        backend.close()


def test_evidence_for_returns_stub_payload_for_qname(
    fake_codegraph_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEGRAPH_DB", str(fake_codegraph_db))
    result = evidence_for(target="pkg.a")
    assert result == {
        "qualified_name": "pkg.a",
        "node_id": "n_a",
        "location": "src/pkg/a.py:1-5",
        "evidence": [],
        "source": "stub",
    }


def test_evidence_for_returns_stub_payload_for_node_id(
    fake_codegraph_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEGRAPH_DB", str(fake_codegraph_db))
    result = evidence_for(target="n_b")
    assert result["qualified_name"] == "pkg.b"
    assert result["node_id"] == "n_b"
    assert result["source"] == "stub"


def test_evidence_for_unknown_symbol_raises(
    fake_codegraph_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEGRAPH_DB", str(fake_codegraph_db))
    with pytest.raises(ValueError, match="Symbol not found"):
        evidence_for(target="pkg.missing")


def test_evidence_for_raises_when_no_codegraph_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEGRAPH_DB", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="No CodeGraph DB"):
        evidence_for(target="anything")


def test_evidence_for_tool_is_registered() -> None:
    import anyio

    tools = anyio.run(mcp.list_tools)
    names = {t.name for t in tools}
    assert "whygraph_evidence_for" in names
