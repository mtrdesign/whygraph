from __future__ import annotations

from pathlib import Path

import pytest

from whygraph.prompts import PROMPT_VERSION, Rationale
from whygraph.rationale import LLMResult, LLMUsage
from whygraph.mcp_server import (
    format_rationale_markdown,
    mcp,
    rationale_pre_edit_brief,
)


_RAT = Rationale(
    purpose="Validates JWT.",
    why="Replaces legacy cookie validator.",
    constraints=["must be sync"],
    tradeoffs=["JWK lookup cached"],
    risks=["claim shape change"],
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


class _CountingLLM:
    def __init__(self, rationale: Rationale = _RAT) -> None:
        self.rationale = rationale
        self.calls = 0
        self.last_user_prompt = ""

    def generate(self, *, system_prompt: str, user_prompt: str, schema=None) -> LLMResult:
        self.calls += 1
        self.last_user_prompt = user_prompt
        return LLMResult(
            rationale=self.rationale,
            model="m",
            backend="fake",
            prompt_version=PROMPT_VERSION,
            usage=LLMUsage(),
        )


def _setup(
    init_git_repo,
    git_commit,
    codegraph_db_factory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    file_content: str = "l1\nl2\nl3\n",
    fake_llm: _CountingLLM | None = None,
) -> tuple[Path, _CountingLLM]:
    repo = init_git_repo()
    git_commit(repo, "src/a.py", file_content, message="init")
    cg_path = codegraph_db_factory(nodes=[_node_dict("src/a.py")], edges=[])
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    # Force the model name to be deterministic across configs.
    monkeypatch.setenv("WHYGRAPH_MODEL", "test-model")
    monkeypatch.chdir(repo)

    llm = fake_llm or _CountingLLM()
    monkeypatch.setattr(
        "whygraph.mcp_server.make_llm_client", lambda config: llm
    )
    return repo, llm


def test_rationale_first_call_generates(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _, llm = _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    result = rationale_pre_edit_brief(target="pkg.a", response_format="json")
    assert isinstance(result, dict)
    assert result["source"] == "generated"
    assert result["purpose"] == _RAT.purpose
    assert result["model"] == "test-model"
    assert "cache_key" in result
    assert "confidence" not in result  # v1 deviation
    assert result["caller_count"] == 0  # _setup builds with edges=[]
    assert result["callee_count"] == 0
    assert llm.calls == 1


def test_rationale_second_call_is_cached(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _, llm = _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    rationale_pre_edit_brief(target="pkg.a", response_format="json")
    second = rationale_pre_edit_brief(target="pkg.a", response_format="json")
    assert second["source"] == "cached"
    assert llm.calls == 1


def test_rationale_force_bypasses_cache(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _, llm = _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    rationale_pre_edit_brief(target="pkg.a", response_format="json")
    forced = rationale_pre_edit_brief(
        target="pkg.a", force=True, response_format="json"
    )
    assert forced["source"] == "generated"
    assert llm.calls == 2


def test_rationale_refresh_evidence_invalidates_rationale(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    repo, llm = _setup(
        init_git_repo, git_commit, codegraph_db_factory, monkeypatch
    )
    first = rationale_pre_edit_brief(target="pkg.a", response_format="json")
    assert first["source"] == "generated"

    # New commit modifies lines 1-3 → blame on the symbol's line range
    # points at a new SHA → bundle_hash changes → cache miss.
    git_commit(repo, "src/a.py", "alpha\nbeta\ngamma\n", message="rewrite")
    second = rationale_pre_edit_brief(
        target="pkg.a", refresh_evidence=True, response_format="json"
    )
    assert second["source"] == "generated"
    assert second["bundle_hash"] != first["bundle_hash"]
    assert llm.calls == 2


def test_rationale_returns_error_when_no_evidence(
    init_git_repo, codegraph_db_factory, monkeypatch
) -> None:
    repo = init_git_repo()
    # File exists but is uncommitted → no blame → empty evidence.
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("uncommitted\n")
    cg_path = codegraph_db_factory(nodes=[_node_dict("src/a.py")], edges=[])
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "whygraph.mcp_server.make_llm_client", lambda config: _CountingLLM()
    )
    with pytest.raises(ValueError, match="No evidence for pkg.a"):
        rationale_pre_edit_brief(target="pkg.a")


def test_rationale_unknown_symbol_raises(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    with pytest.raises(ValueError, match="Symbol not found"):
        rationale_pre_edit_brief(target="pkg.nope")


def test_rationale_markdown_format_omits_confidence(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _setup(init_git_repo, git_commit, codegraph_db_factory, monkeypatch)
    text = rationale_pre_edit_brief(target="pkg.a")  # markdown is default
    assert isinstance(text, str)
    assert text.startswith("# Rationale: `pkg.a`")
    assert "## Purpose" in text
    assert "## Why" in text
    assert "## Constraints" in text
    assert "Confidence" not in text  # v1 deviation


def test_rationale_markdown_renders_empty_lists_as_none() -> None:
    from whygraph.backend import SymbolNode
    from whygraph.cochange.types import CoChangeReport, VolatilityReport
    from whygraph.context import RationaleContext
    from whygraph.evidence.types import CollectionResult
    from whygraph.neighbors import RationaleNeighbors
    from whygraph.rationale import RationaleRecord, cache_key

    node = SymbolNode(
        id="n_x",
        kind="function",
        name="x",
        qualified_name="pkg.x",
        file_path="src/x.py",
        language="python",
        start_line=1,
        end_line=2,
        docstring=None,
        signature=None,
    )
    collection = CollectionResult(
        evidence=[],
        bundle_hash="0" * 64,
        source="cache",
        collected_at=0,
        head_at_collection=None,
    )
    record = RationaleRecord(
        node_id="n_x",
        bundle_hash="0" * 64,
        prompt_version=PROMPT_VERSION,
        model="m",
        purpose="",
        why="",
        constraints=[],
        tradeoffs=[],
        risks=[],
        generated_at=0,
        cache_key=cache_key("pkg.x", "src/x.py", PROMPT_VERSION, "m", "0" * 64),
    )
    context = RationaleContext(
        neighbors=RationaleNeighbors([], [], 0, 0),
        cochange=CoChangeReport(
            target_file="src/x.py",
            head_sha="",
            commits_considered=0,
            neighbors=[],
            truncated=0,
        ),
        volatility=VolatilityReport(
            target_file="src/x.py",
            head_sha="",
            commits_total=0,
            commits_90d=0,
            commits_180d=0,
            commits_365d=0,
            distinct_authors=0,
            days_since_last_change=None,
        ),
    )
    text = format_rationale_markdown(node, collection, record, "cached", context)
    # All five rationale sections fall back to _(none)_; the Context and
    # Volatility lines use different sentinels, so the count stays at 5.
    assert text.count("_(none)_") == 5


def test_both_tools_are_registered() -> None:
    import anyio

    tools = anyio.run(mcp.list_tools)
    names = {t.name for t in tools}
    assert "whygraph_evidence_for" in names
    assert "whygraph_rationale_pre_edit_brief" in names


# ---------------------------------------------------------------------------
# Neighbor enrichment (v4 prompt) — flow-through + cache invalidation.
# ---------------------------------------------------------------------------


def _setup_with_caller(
    init_git_repo,
    git_commit,
    codegraph_db_factory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    caller_signature: str,
):
    """Like _setup, but seeds CodeGraph with a caller node + 'calls' edge."""
    repo = init_git_repo()
    git_commit(repo, "src/a.py", "l1\nl2\nl3\n", message="init")

    target = _node_dict("src/a.py", "pkg.a", "n_a")
    caller = _node_dict("src/caller.py", "pkg.special_caller", "n_c")
    caller["signature"] = caller_signature

    cg_path = codegraph_db_factory(
        nodes=[target, caller],
        edges=[("n_c", "n_a", "calls")],
    )
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WHYGRAPH_DB", raising=False)
    monkeypatch.setenv("WHYGRAPH_MODEL", "test-model")
    monkeypatch.chdir(repo)

    llm = _CountingLLM()
    monkeypatch.setattr(
        "whygraph.mcp_server.make_llm_client", lambda config: llm
    )
    return repo, llm


def test_rationale_prompt_includes_caller_qualified_name(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    _, llm = _setup_with_caller(
        init_git_repo,
        git_commit,
        codegraph_db_factory,
        monkeypatch,
        caller_signature="def special_caller()",
    )
    result = rationale_pre_edit_brief(target="pkg.a", response_format="json")
    assert result["caller_count"] == 1
    assert "pkg.special_caller" in llm.last_user_prompt
    assert "Callers (1" in llm.last_user_prompt


def test_rationale_invalidates_when_caller_signature_changes(
    init_git_repo, git_commit, codegraph_db_factory, monkeypatch
) -> None:
    """A meaningful change to a caller's signature should bust the cache.

    Mechanism: collect_neighbors picks up the new signature; neighbor
    fingerprint changes; combine_bundle_hash returns a different combined
    hash; rationale store lookup misses; LLM is called again.
    """
    from whygraph import mcp_server

    repo, llm = _setup_with_caller(
        init_git_repo,
        git_commit,
        codegraph_db_factory,
        monkeypatch,
        caller_signature="def special_caller()",
    )
    first = rationale_pre_edit_brief(target="pkg.a", response_format="json")
    assert first["source"] == "generated"
    assert llm.calls == 1

    # Swap to a CodeGraph DB where the caller signature differs. Need to
    # close + reopen the backend so the cached deps pick up the new path.
    mcp_server._reset_deps()
    target = _node_dict("src/a.py", "pkg.a", "n_a")
    caller_v2 = _node_dict("src/caller.py", "pkg.special_caller", "n_c")
    caller_v2["signature"] = "def special_caller(x: int)"
    cg_v2 = codegraph_db_factory(
        nodes=[target, caller_v2],
        edges=[("n_c", "n_a", "calls")],
    )
    monkeypatch.setenv("CODEGRAPH_DB", str(cg_v2))

    second = rationale_pre_edit_brief(target="pkg.a", response_format="json")
    assert second["source"] == "generated"
    assert llm.calls == 2
    assert second["bundle_hash"] != first["bundle_hash"]
    # New caller signature is in the new prompt.
    assert "def special_caller(x: int)" in llm.last_user_prompt
