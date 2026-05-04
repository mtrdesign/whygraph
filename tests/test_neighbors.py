from __future__ import annotations

from whygraph.backend import SymbolNode
from whygraph.neighbors import (
    DEFAULT_NEIGHBOR_LIMIT,
    RationaleNeighbors,
    collect_neighbors,
    combine_bundle_hash,
    neighbor_fingerprint,
)


def _node(
    qname: str,
    *,
    node_id: str | None = None,
    signature: str | None = None,
    docstring: str | None = None,
) -> SymbolNode:
    return SymbolNode(
        id=node_id or f"id_{qname.replace('.', '_')}",
        kind="function",
        name=qname.rsplit(".", 1)[-1],
        qualified_name=qname,
        file_path=f"src/{qname.replace('.', '/')}.py",
        language="python",
        start_line=1,
        end_line=5,
        docstring=docstring,
        signature=signature,
    )


class _FakeBackend:
    """Just enough GraphBackend to drive collect_neighbors."""

    def __init__(
        self,
        *,
        callers: dict[str, list[SymbolNode]] | None = None,
        callees: dict[str, list[SymbolNode]] | None = None,
    ) -> None:
        self._callers = callers or {}
        self._callees = callees or {}

    def get_callers(self, node_id: str) -> list[SymbolNode]:
        return list(self._callers.get(node_id, []))

    def get_callees(self, node_id: str) -> list[SymbolNode]:
        return list(self._callees.get(node_id, []))

    # Unused by collect_neighbors but required by the Protocol.
    def get_node(self, qualified_name): ...
    def get_node_by_id(self, node_id): ...
    def find_symbols(self, query, limit=20): ...
    def walk_neighbors(self, node_id, depth=1): ...
    def close(self): ...


# ---------------------------------------------------------------------------
# collect_neighbors
# ---------------------------------------------------------------------------


def test_collect_neighbors_sorts_by_qualified_name() -> None:
    backend = _FakeBackend(
        callers={"target": [_node("pkg.zeta"), _node("pkg.alpha"), _node("pkg.middle")]},
        callees={"target": [_node("pkg.zeta"), _node("pkg.alpha")]},
    )
    n = collect_neighbors(backend, "target")
    assert [c.qualified_name for c in n.callers] == ["pkg.alpha", "pkg.middle", "pkg.zeta"]
    assert [c.qualified_name for c in n.callees] == ["pkg.alpha", "pkg.zeta"]


def test_collect_neighbors_truncates_at_limit() -> None:
    callers = [_node(f"pkg.c{i:02d}") for i in range(12)]
    backend = _FakeBackend(callers={"t": callers})
    n = collect_neighbors(backend, "t", limit=8)
    assert len(n.callers) == 8
    assert n.truncated_callers == 4
    # Sorted, so the included ones are c00..c07.
    assert [c.qualified_name for c in n.callers] == [f"pkg.c{i:02d}" for i in range(8)]


def test_collect_neighbors_zero_truncation_when_under_limit() -> None:
    backend = _FakeBackend(callers={"t": [_node("pkg.a"), _node("pkg.b")]})
    n = collect_neighbors(backend, "t", limit=8)
    assert n.truncated_callers == 0
    assert n.truncated_callees == 0


def test_collect_neighbors_empty_for_isolated_node() -> None:
    backend = _FakeBackend()
    n = collect_neighbors(backend, "loner")
    assert n.callers == []
    assert n.callees == []
    assert n.truncated_callers == 0
    assert n.truncated_callees == 0
    assert n.is_empty


def test_collect_neighbors_uses_default_limit() -> None:
    callers = [_node(f"pkg.c{i:02d}") for i in range(20)]
    backend = _FakeBackend(callers={"t": callers})
    n = collect_neighbors(backend, "t")  # no limit kwarg
    assert len(n.callers) == DEFAULT_NEIGHBOR_LIMIT
    assert n.truncated_callers == 20 - DEFAULT_NEIGHBOR_LIMIT


# ---------------------------------------------------------------------------
# neighbor_fingerprint
# ---------------------------------------------------------------------------


def _neighbors(
    callers: list[SymbolNode] | None = None,
    callees: list[SymbolNode] | None = None,
    *,
    truncated_callers: int = 0,
    truncated_callees: int = 0,
) -> RationaleNeighbors:
    return RationaleNeighbors(
        callers=callers or [],
        callees=callees or [],
        truncated_callers=truncated_callers,
        truncated_callees=truncated_callees,
    )


def test_fingerprint_is_stable_for_same_inputs() -> None:
    a = _neighbors(callers=[_node("pkg.a", signature="def a()")])
    b = _neighbors(callers=[_node("pkg.a", signature="def a()")])
    assert neighbor_fingerprint(a) == neighbor_fingerprint(b)


def test_fingerprint_changes_when_caller_signature_changes() -> None:
    before = _neighbors(callers=[_node("pkg.a", signature="def a()")])
    after = _neighbors(callers=[_node("pkg.a", signature="def a(x: int)")])
    assert neighbor_fingerprint(before) != neighbor_fingerprint(after)


def test_fingerprint_changes_when_caller_docstring_changes() -> None:
    before = _neighbors(callers=[_node("pkg.a", docstring="old doc")])
    after = _neighbors(callers=[_node("pkg.a", docstring="new doc")])
    assert neighbor_fingerprint(before) != neighbor_fingerprint(after)


def test_fingerprint_changes_when_caller_added() -> None:
    before = _neighbors(callers=[_node("pkg.a")])
    after = _neighbors(callers=[_node("pkg.a"), _node("pkg.b")])
    assert neighbor_fingerprint(before) != neighbor_fingerprint(after)


def test_fingerprint_distinguishes_caller_from_callee() -> None:
    """Same SymbolNode in opposite directions → different fingerprint."""
    sym = _node("pkg.a")
    a = _neighbors(callers=[sym])
    b = _neighbors(callees=[sym])
    assert neighbor_fingerprint(a) != neighbor_fingerprint(b)


def test_fingerprint_ignores_truncated_count_field() -> None:
    """truncated_* are reporting fields, not part of the cache identity.

    Two RationaleNeighbors with the same shown lists should fingerprint
    identically, even if one has more truncated tail than the other (e.g.
    two snapshots taken at different times where only off-screen neighbors
    changed).
    """
    a = _neighbors(callers=[_node("pkg.a")], truncated_callers=0)
    b = _neighbors(callers=[_node("pkg.a")], truncated_callers=5)
    assert neighbor_fingerprint(a) == neighbor_fingerprint(b)


def test_fingerprint_empty_neighbors_is_deterministic() -> None:
    assert neighbor_fingerprint(_neighbors()) == neighbor_fingerprint(_neighbors())
    # Empty SHA-256 hash, since no bytes are ever fed in.
    assert (
        neighbor_fingerprint(_neighbors())
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


# ---------------------------------------------------------------------------
# combine_bundle_hash
# ---------------------------------------------------------------------------


def test_combine_bundle_hash_is_deterministic() -> None:
    assert combine_bundle_hash("a" * 64, "b" * 64) == combine_bundle_hash("a" * 64, "b" * 64)


def test_combine_bundle_hash_differs_from_either_input() -> None:
    ev = "a" * 64
    nb = "b" * 64
    combined = combine_bundle_hash(ev, nb)
    assert combined != ev
    assert combined != nb


def test_combine_bundle_hash_changes_when_evidence_hash_changes() -> None:
    nb = "b" * 64
    assert combine_bundle_hash("a" * 64, nb) != combine_bundle_hash("c" * 64, nb)


def test_combine_bundle_hash_changes_when_neighbor_hash_changes() -> None:
    ev = "a" * 64
    assert combine_bundle_hash(ev, "b" * 64) != combine_bundle_hash(ev, "c" * 64)
