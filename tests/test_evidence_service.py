from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from whygraph.backend import SymbolNode
from whygraph.db import open_whygraph_db
from whygraph.evidence import (
    EvidenceRow,
    EvidenceService,
    EvidenceStore,
    compute_bundle_hash,
)


# ---------------------------------------------------------------------------
# compute_bundle_hash
# ---------------------------------------------------------------------------


def test_compute_bundle_hash_stable_under_row_reorder() -> None:
    a = EvidenceRow(source="git_blame", ref="abc", payload={"x": 1, "y": 2})
    b = EvidenceRow(source="git_commit", ref="abc", payload={"subject": "fix"})
    c = EvidenceRow(source="pr", ref="42", payload={"title": "compliance"})
    assert compute_bundle_hash([a, b, c]) == compute_bundle_hash([c, b, a])


def test_compute_bundle_hash_stable_under_payload_key_order() -> None:
    a = EvidenceRow(source="pr", ref="1", payload={"a": 1, "b": 2})
    b = EvidenceRow(source="pr", ref="1", payload={"b": 2, "a": 1})
    assert compute_bundle_hash([a]) == compute_bundle_hash([b])


def test_compute_bundle_hash_stable_under_nested_key_order() -> None:
    a = EvidenceRow(source="pr", ref="1", payload={"meta": {"x": 1, "y": 2}})
    b = EvidenceRow(source="pr", ref="1", payload={"meta": {"y": 2, "x": 1}})
    assert compute_bundle_hash([a]) == compute_bundle_hash([b])


def test_compute_bundle_hash_changes_when_payload_changes() -> None:
    a = EvidenceRow(source="git_commit", ref="abc", payload={"subject": "old"})
    b = EvidenceRow(source="git_commit", ref="abc", payload={"subject": "new"})
    assert compute_bundle_hash([a]) != compute_bundle_hash([b])


def test_compute_bundle_hash_changes_when_ref_changes() -> None:
    a = EvidenceRow(source="git_commit", ref="abc", payload={"x": 1})
    b = EvidenceRow(source="git_commit", ref="def", payload={"x": 1})
    assert compute_bundle_hash([a]) != compute_bundle_hash([b])


def test_compute_bundle_hash_handles_null_ref() -> None:
    rows = [EvidenceRow(source="docstring", ref=None, payload={"text": "hi"})]
    h = compute_bundle_hash(rows)
    assert isinstance(h, str)
    assert len(h) == 64


def test_compute_bundle_hash_empty_rows_is_deterministic() -> None:
    assert compute_bundle_hash([]) == compute_bundle_hash([])
    assert (
        compute_bundle_hash([])
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


# ---------------------------------------------------------------------------
# EvidenceStore
# ---------------------------------------------------------------------------


def test_store_replace_then_for_node_roundtrip(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = EvidenceStore(conn)
        rows = [
            EvidenceRow(
                source="git_blame", ref="abc", payload={"author": "Alice"}
            ),
            EvidenceRow(
                source="git_commit", ref="abc", payload={"subject": "fix"}
            ),
        ]
        store.replace("n1", "pkg.fn", rows, "head_sha", now=100)
        records = store.for_node("n1")
        assert [r.source for r in records] == ["git_blame", "git_commit"]
        assert records[0].payload["author"] == "Alice"
        assert records[0].collected_at == 100
        meta = store.bundle_meta_for("n1")
        assert meta is not None
        assert meta.head_at_collection == "head_sha"
        assert meta.built_at == 100
    finally:
        conn.close()


def test_store_replace_purges_existing_evidence(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = EvidenceStore(conn)
        store.replace(
            "n1",
            "pkg.fn",
            [EvidenceRow(source="git_commit", ref="old", payload={})],
            None,
            now=100,
        )
        store.replace(
            "n1",
            "pkg.fn",
            [EvidenceRow(source="git_commit", ref="new", payload={})],
            None,
            now=200,
        )
        records = store.for_node("n1")
        assert len(records) == 1
        assert records[0].ref == "new"
    finally:
        conn.close()


def test_store_bundle_meta_returns_none_for_unknown(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = EvidenceStore(conn)
        assert store.bundle_meta_for("nope") is None
    finally:
        conn.close()


def test_store_replace_returns_bundle_hash(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = EvidenceStore(conn)
        rows = [EvidenceRow(source="pr", ref="1", payload={"title": "x"})]
        h = store.replace("n1", "pkg.fn", rows, None, now=100)
        assert h == compute_bundle_hash(rows)
        assert store.bundle_meta_for("n1").bundle_hash == h
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# EvidenceService
# ---------------------------------------------------------------------------


def _node(file_path: str = "src/a.py", node_id: str = "n_a") -> SymbolNode:
    return SymbolNode(
        id=node_id,
        kind="function",
        name="a",
        qualified_name="pkg.a",
        file_path=file_path,
        language="python",
        start_line=1,
        end_line=3,
        docstring=None,
        signature=None,
    )


class FakeGitCollector:
    """Minimal stand-in shaped like GitEvidenceCollector for service tests."""

    def __init__(self, blame_entries=None, commits=None) -> None:
        self.blame_entries = blame_entries or []
        self.commits = commits or {}

    def blame_line_range(self, file_path, start, end):
        return list(self.blame_entries)

    def commit_info(self, sha):
        return self.commits.get(sha)

    def file_head_sha(self, file_path):
        return None  # overridden via service injection in tests


class FakeGitHubCollector:
    def is_available(self) -> bool:
        return False


def _service(
    tmp_path: Path,
    *,
    ttl_seconds: int = 3600,
    now_value: int = 1000,
    head_sha: str | None = "HEAD_A",
) -> tuple[EvidenceService, EvidenceStore, dict[str, Any]]:
    """Return (service, store, mutable_state) where mutable_state lets tests
    advance time and change the file HEAD sha between calls."""
    conn = open_whygraph_db(tmp_path / "wg.db")
    store = EvidenceStore(conn)
    state: dict[str, Any] = {"now": now_value, "head": head_sha}
    fake_git = FakeGitCollector(
        blame_entries=[],  # collect returns [] so we get an empty bundle
    )
    service = EvidenceService(
        store,
        fake_git,  # type: ignore[arg-type]
        FakeGitHubCollector(),  # type: ignore[arg-type]
        tmp_path,
        ttl_seconds,
        now=lambda: state["now"],
        head_sha_fn=lambda file_path: state["head"],
    )
    return service, store, state


def test_service_first_call_collects_and_stores(tmp_path: Path) -> None:
    service, store, _ = _service(tmp_path)
    result = service.for_node(_node())
    assert result.source == "collected"
    assert result.head_at_collection == "HEAD_A"
    assert store.bundle_meta_for("n_a") is not None


def test_service_returns_cache_when_fresh(tmp_path: Path) -> None:
    service, _, state = _service(tmp_path)
    service.for_node(_node())
    state["now"] += 60  # 1 minute later, well within TTL
    result = service.for_node(_node())
    assert result.source == "cache"


def test_service_recollects_when_ttl_expired(tmp_path: Path) -> None:
    service, _, state = _service(tmp_path, ttl_seconds=10)
    service.for_node(_node())
    state["now"] += 60  # past TTL
    result = service.for_node(_node())
    assert result.source == "collected"


def test_service_recollects_when_head_sha_changed(tmp_path: Path) -> None:
    service, _, state = _service(tmp_path)
    service.for_node(_node())
    state["head"] = "HEAD_B"
    result = service.for_node(_node())
    assert result.source == "collected"
    assert result.head_at_collection == "HEAD_B"


def test_service_force_bypasses_cache(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    service.for_node(_node())
    result = service.for_node(_node(), force=True)
    assert result.source == "collected"


def test_service_trusts_ttl_when_head_at_collection_is_null(
    tmp_path: Path,
) -> None:
    # First collection with no HEAD (file untracked) → head_at_collection NULL.
    # Subsequent calls within TTL should hit cache even if the file *now* has
    # commits (the v0 comment about wasted recollection).
    service, _, state = _service(tmp_path, head_sha=None)
    service.for_node(_node())
    state["head"] = "HEAD_X"  # file was just committed
    result = service.for_node(_node())
    assert result.source == "cache"


def test_service_cache_miss_when_no_prior_collection(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    result = service.for_node(_node())
    assert result.source == "collected"


def test_service_collected_at_matches_now_at_collect_time(
    tmp_path: Path,
) -> None:
    service, _, state = _service(tmp_path, now_value=42)
    result = service.for_node(_node())
    assert result.collected_at == 42
    state["now"] = 1234
    state["head"] = "HEAD_NEW"  # invalidate cache
    result2 = service.for_node(_node())
    assert result2.collected_at == 1234


def test_service_uses_real_git_evidence_when_blame_present(
    tmp_path: Path,
) -> None:
    """Sanity: collect_git_evidence is called, not just stub returning []."""
    from whygraph.evidence import GitBlameEntry, GitCommitInfo

    conn = open_whygraph_db(tmp_path / "wg.db")
    store = EvidenceStore(conn)
    fake_git = FakeGitCollector(
        blame_entries=[
            GitBlameEntry(
                commit="abc123",
                author="Alice",
                author_email="a@x",
                author_time=100,
                summary="initial",
                line_count=3,
            )
        ],
        commits={
            "abc123": GitCommitInfo(
                sha="abc123",
                author="Alice",
                author_email="a@x",
                author_time=100,
                committer="Alice",
                committer_email="a@x",
                committer_time=100,
                parents=(),
                subject="initial",
                body="",
            )
        },
    )
    state: dict[str, Any] = {"now": 1000, "head": "HEAD_A"}
    service = EvidenceService(
        store,
        fake_git,  # type: ignore[arg-type]
        None,
        tmp_path,
        3600,
        now=lambda: state["now"],
        head_sha_fn=lambda fp: state["head"],
    )
    result = service.for_node(_node())
    sources = {e.source for e in result.evidence}
    assert sources == {"git_blame", "git_commit"}
    assert result.source == "collected"
