from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.backend import SymbolNode
from whygraph.db import open_whygraph_db
from whygraph.evidence.types import EvidenceRecord
from whygraph.neighbors import RationaleNeighbors
from whygraph.prompts import PROMPT_VERSION, Rationale
from whygraph.rationale import (
    LLMResult,
    LLMUsage,
    RationaleService,
    RationaleStore,
    cache_key,
)

_NO_NEIGHBORS = RationaleNeighbors(callers=[], callees=[], truncated_callers=0, truncated_callees=0)


_RAT = Rationale(
    purpose="Validates JWT.",
    why="Replaces legacy cookie validator.",
    constraints=["must be sync"],
    tradeoffs=["JWK lookup cached"],
    risks=["claim shape change"],
)


def _node(node_id: str = "n_a", qname: str = "pkg.a", file_path: str = "src/a.py") -> SymbolNode:
    return SymbolNode(
        id=node_id,
        kind="function",
        name=qname.rsplit(".", 1)[-1],
        qualified_name=qname,
        file_path=file_path,
        language="python",
        start_line=1,
        end_line=10,
        docstring=None,
        signature=None,
    )


# ---------------------------------------------------------------------------
# cache_key
# ---------------------------------------------------------------------------


def test_cache_key_is_stable_for_same_inputs() -> None:
    a = cache_key("pkg.a", "src/a.py", "v3", "sonnet", "deadbeef")
    b = cache_key("pkg.a", "src/a.py", "v3", "sonnet", "deadbeef")
    assert a == b
    assert len(a) == 64


def test_cache_key_changes_when_qname_changes() -> None:
    a = cache_key("pkg.a", "src/a.py", "v3", "sonnet", "deadbeef")
    b = cache_key("pkg.b", "src/a.py", "v3", "sonnet", "deadbeef")
    assert a != b


def test_cache_key_changes_when_file_path_changes() -> None:
    a = cache_key("pkg.a", "src/a.py", "v3", "sonnet", "deadbeef")
    b = cache_key("pkg.a", "src/other.py", "v3", "sonnet", "deadbeef")
    assert a != b


def test_cache_key_changes_when_prompt_version_changes() -> None:
    a = cache_key("pkg.a", "src/a.py", "v3", "sonnet", "deadbeef")
    b = cache_key("pkg.a", "src/a.py", "v4", "sonnet", "deadbeef")
    assert a != b


def test_cache_key_changes_when_model_changes() -> None:
    a = cache_key("pkg.a", "src/a.py", "v3", "sonnet", "deadbeef")
    b = cache_key("pkg.a", "src/a.py", "v3", "opus", "deadbeef")
    assert a != b


def test_cache_key_changes_when_bundle_hash_changes() -> None:
    a = cache_key("pkg.a", "src/a.py", "v3", "sonnet", "deadbeef")
    b = cache_key("pkg.a", "src/a.py", "v3", "sonnet", "feedface")
    assert a != b


# ---------------------------------------------------------------------------
# RationaleStore
# ---------------------------------------------------------------------------


def test_upsert_then_get_roundtrip(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = RationaleStore(conn)
        store.upsert(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b1",
            prompt_version="v3",
            model="m",
            rationale=_RAT,
            now=100,
        )
        got = store.get(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b1",
            prompt_version="v3",
            model="m",
        )
        assert got is not None
        assert got.purpose == _RAT.purpose
        assert got.constraints == _RAT.constraints
        assert got.generated_at == 100
        assert got.cache_key == cache_key(
            "pkg.a", "src/a.py", "v3", "m", "b1"
        )
    finally:
        conn.close()


def test_get_returns_none_when_bundle_hash_differs(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = RationaleStore(conn)
        store.upsert(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b1",
            prompt_version="v3",
            model="m",
            rationale=_RAT,
            now=100,
        )
        assert (
            store.get(
                qualified_name="pkg.a",
                file_path="src/a.py",
                node_id="n_a",
                bundle_hash="b2",
                prompt_version="v3",
                model="m",
            )
            is None
        )
    finally:
        conn.close()


def test_get_returns_none_when_prompt_version_differs(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = RationaleStore(conn)
        store.upsert(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b1",
            prompt_version="v3",
            model="m",
            rationale=_RAT,
            now=100,
        )
        assert (
            store.get(
                qualified_name="pkg.a",
                file_path="src/a.py",
                node_id="n_a",
                bundle_hash="b1",
                prompt_version="v4",
                model="m",
            )
            is None
        )
    finally:
        conn.close()


def test_get_returns_none_when_model_differs(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = RationaleStore(conn)
        store.upsert(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b1",
            prompt_version="v3",
            model="sonnet",
            rationale=_RAT,
            now=100,
        )
        assert (
            store.get(
                qualified_name="pkg.a",
                file_path="src/a.py",
                node_id="n_a",
                bundle_hash="b1",
                prompt_version="v3",
                model="opus",
            )
            is None
        )
    finally:
        conn.close()


def test_get_returns_none_for_unknown_node(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = RationaleStore(conn)
        assert (
            store.get(
                qualified_name="pkg.a",
                file_path="src/a.py",
                node_id="missing",
                bundle_hash="b1",
                prompt_version="v3",
                model="m",
            )
            is None
        )
    finally:
        conn.close()


def test_upsert_replaces_existing_row(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = RationaleStore(conn)
        store.upsert(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b1",
            prompt_version="v3",
            model="m",
            rationale=_RAT,
            now=100,
        )
        new = Rationale(
            purpose="updated",
            why="new",
            constraints=[],
            tradeoffs=[],
            risks=[],
        )
        store.upsert(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b2",
            prompt_version="v3",
            model="m",
            rationale=new,
            now=200,
        )
        got = store.get(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b2",
            prompt_version="v3",
            model="m",
        )
        assert got is not None
        assert got.purpose == "updated"
        assert got.generated_at == 200
        # Old bundle_hash should not match anymore.
        assert (
            store.get(
                qualified_name="pkg.a",
                file_path="src/a.py",
                node_id="n_a",
                bundle_hash="b1",
                prompt_version="v3",
                model="m",
            )
            is None
        )
    finally:
        conn.close()


def test_confidence_column_is_null_after_upsert(tmp_path: Path) -> None:
    conn = open_whygraph_db(tmp_path / "wg.db")
    try:
        store = RationaleStore(conn)
        store.upsert(
            qualified_name="pkg.a",
            file_path="src/a.py",
            node_id="n_a",
            bundle_hash="b1",
            prompt_version="v3",
            model="m",
            rationale=_RAT,
            now=100,
        )
        row = conn.execute(
            "SELECT confidence FROM rationale WHERE node_id = ?", ("n_a",)
        ).fetchone()
        assert row["confidence"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# RationaleService.get_or_generate
# ---------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, rationale: Rationale = _RAT) -> None:
        self.rationale = rationale
        self.calls: list[dict] = []

    def generate(self, *, system_prompt: str, user_prompt: str, schema=None) -> LLMResult:
        self.calls.append({"system": system_prompt, "user": user_prompt})
        return LLMResult(
            rationale=self.rationale,
            model="m",
            backend="fake",
            prompt_version=PROMPT_VERSION,
            usage=LLMUsage(),
        )


def _service(tmp_path: Path, llm: _FakeLLM | None = None) -> tuple[RationaleService, _FakeLLM]:
    conn = open_whygraph_db(tmp_path / "wg.db")
    fake = llm or _FakeLLM()
    service = RationaleService(
        RationaleStore(conn),
        fake,
        model="m",
        now=lambda: 1000,
    )
    return service, fake


def test_service_first_call_generates(tmp_path: Path) -> None:
    service, fake = _service(tmp_path)
    rec, source = service.get_or_generate(_node(), [], _NO_NEIGHBORS, "b1")
    assert source == "generated"
    assert rec.bundle_hash == "b1"
    assert rec.purpose == _RAT.purpose
    assert len(fake.calls) == 1


def test_service_second_call_is_cached(tmp_path: Path) -> None:
    service, fake = _service(tmp_path)
    service.get_or_generate(_node(), [], _NO_NEIGHBORS, "b1")
    rec, source = service.get_or_generate(_node(), [], _NO_NEIGHBORS, "b1")
    assert source == "cached"
    assert len(fake.calls) == 1  # LLM not called again
    assert rec.bundle_hash == "b1"


def test_service_force_bypasses_cache(tmp_path: Path) -> None:
    service, fake = _service(tmp_path)
    service.get_or_generate(_node(), [], _NO_NEIGHBORS, "b1")
    _, source = service.get_or_generate(_node(), [], _NO_NEIGHBORS, "b1", force=True)
    assert source == "generated"
    assert len(fake.calls) == 2


def test_service_bundle_hash_change_invalidates_cache(tmp_path: Path) -> None:
    service, fake = _service(tmp_path)
    service.get_or_generate(_node(), [], _NO_NEIGHBORS, "b1")
    _, source = service.get_or_generate(_node(), [], _NO_NEIGHBORS, "b2")
    assert source == "generated"
    assert len(fake.calls) == 2


def test_service_passes_user_prompt_to_llm(tmp_path: Path) -> None:
    service, fake = _service(tmp_path)
    evidence = [
        EvidenceRecord(
            id=1,
            node_id="n_a",
            qualified_name="pkg.a",
            source="git_commit",
            ref="abc",
            payload={"subject": "fix bug", "author_time": 1700000000, "author": "Alice"},
            collected_at=0,
        )
    ]
    service.get_or_generate(_node(), evidence, _NO_NEIGHBORS, "b1")
    user = fake.calls[0]["user"]
    assert "Symbol: pkg.a" in user
    assert "fix bug" in user


def test_service_uses_stored_now_for_generated_at(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    rec, _ = service.get_or_generate(_node(), [], _NO_NEIGHBORS, "b1")
    assert rec.generated_at == 1000
