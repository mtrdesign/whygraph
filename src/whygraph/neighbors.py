from __future__ import annotations

import hashlib
from dataclasses import dataclass

from whygraph.backend import GraphBackend, SymbolNode

DEFAULT_NEIGHBOR_LIMIT = 8


@dataclass(frozen=True)
class RationaleNeighbors:
    callers: list[SymbolNode]
    callees: list[SymbolNode]
    truncated_callers: int
    truncated_callees: int

    @property
    def is_empty(self) -> bool:
        return not self.callers and not self.callees


def collect_neighbors(
    backend: GraphBackend,
    node_id: str,
    *,
    limit: int = DEFAULT_NEIGHBOR_LIMIT,
) -> RationaleNeighbors:
    callers = sorted(backend.get_callers(node_id), key=lambda n: n.qualified_name)
    callees = sorted(backend.get_callees(node_id), key=lambda n: n.qualified_name)
    return RationaleNeighbors(
        callers=callers[:limit],
        callees=callees[:limit],
        truncated_callers=max(0, len(callers) - limit),
        truncated_callees=max(0, len(callees) - limit),
    )


def neighbor_fingerprint(n: RationaleNeighbors) -> str:
    """Hash the *included* neighbors only.

    Sorting happens upstream in `collect_neighbors`, so the input lists are
    already deterministic. Only the post-truncate slice contributes — a
    change in a neighbor that fell outside the limit doesn't invalidate the
    rationale (it wasn't shown to the LLM anyway).
    """
    h = hashlib.sha256()
    for direction, items in (("caller", n.callers), ("callee", n.callees)):
        for sym in items:
            h.update(direction.encode("utf-8"))
            h.update(b"\0")
            h.update(sym.qualified_name.encode("utf-8"))
            h.update(b"\0")
            h.update((sym.signature or "").encode("utf-8"))
            h.update(b"\0")
            h.update((sym.docstring or "").encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


def combine_bundle_hash(evidence_hash: str, neighbors_hash: str) -> str:
    """Fold evidence + neighbor fingerprints into a single rationale cache key.

    Kept separate from `compute_bundle_hash` (which hashes raw evidence rows)
    so the evidence-cache layer never sees neighbor data.
    """
    return hashlib.sha256(
        f"{evidence_hash}|{neighbors_hash}".encode("utf-8")
    ).hexdigest()
