from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvidenceRow:
    source: str
    ref: str | None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceRecord:
    id: int
    node_id: str
    qualified_name: str
    source: str
    ref: str | None
    payload: Any
    collected_at: int


@dataclass(frozen=True)
class BundleMeta:
    bundle_hash: str
    built_at: int
    head_at_collection: str | None


@dataclass(frozen=True)
class CollectionResult:
    evidence: list[EvidenceRecord]
    bundle_hash: str
    source: str  # "cache" | "collected"
    collected_at: int
    head_at_collection: str | None


def _stable_json(value: Any) -> str:
    # json.dumps with sort_keys recursively sorts dict keys at every depth,
    # matching v0's hand-rolled stableStringify for our payload shapes.
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compute_bundle_hash(rows: list[EvidenceRow]) -> str:
    sorted_rows = sorted(rows, key=lambda r: (r.source, r.ref or ""))
    h = hashlib.sha256()
    for r in sorted_rows:
        h.update(r.source.encode("utf-8"))
        h.update(b"|")
        h.update((r.ref or "").encode("utf-8"))
        h.update(b"|")
        h.update(_stable_json(r.payload).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()
