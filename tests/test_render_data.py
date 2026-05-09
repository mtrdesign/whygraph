"""End-to-end test for `whygraph.render.data.assemble`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whygraph.render import data as data_module
from whygraph.scan import authors as authors_module
from whygraph.scan.db import Database
from whygraph.scan.git import Commit
from whygraph.scan.github import PullRequest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    """Init a real git repo with src/pkg/{a,b,c}.py and a WhyGraph DB seeded
    with commits + PR + author. Pair with the `fake_codegraph_db` fixture
    (whose default nodes match these paths)."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "alice@example.com")
    _git(tmp_path, "config", "user.name", "Alice")
    _git(tmp_path, "config", "commit.gpgsign", "false")

    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    (pkg / "b.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    (pkg / "c.py").write_text("L1\nL2\nL3\nL4\nL5\n")
    _git(tmp_path, "add", "src/pkg/a.py", "src/pkg/b.py", "src/pkg/c.py")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    sha = _git_out(tmp_path, "rev-parse", "HEAD")

    db_path = tmp_path / ".whygraph" / "whygraph.db"
    with Database(db_path) as db:
        db.upsert_commit(
            Commit(
                sha=sha,
                parent_shas=[],
                author_name="Alice",
                author_email="alice@example.com",
                authored_at="2026-04-01T00:00:00+00:00",
                committed_at="2026-04-01T00:00:00+00:00",
                subject="initial",
                body="rationale body",
                files_changed=3,
                insertions=15,
                deletions=0,
            )
        )
        db.set_llm_description(sha, "added pkg/{a,b,c}.py", "haiku")
        # PR linking sha → number 42 with a label.
        db.upsert_pull_request(
            PullRequest(
                number=42,
                title="Add pkg",
                body="b",
                state="closed",
                draft=False,
                created_at="2026-04-01T00:00:00+00:00",
                updated_at="2026-04-01T00:00:00+00:00",
                closed_at=None,
                merged_at="2026-04-01T00:00:00+00:00",
                merge_commit_sha=sha,
                head_sha="0" * 40,
                head_ref="feat",
                base_ref="main",
                author="alice",
                html_url="https://github.com/o/r/pull/42",
                labels=["feature"],
                commit_titles=[
                    {
                        "oid": sha,
                        "headline": "initial",
                        "author_login": "alice",
                        "author_name": "Alice",
                        "author_email": "alice@example.com",
                    }
                ],
            )
        )
        # Cached rationale for pkg.a only — covers the has_rationale path.
        db.set_rationale_cache(
            cache_key="cache-pkg-a",
            target_qualified_name="pkg.a",
            target_path="src/pkg/a.py",
            target_line_start=1,
            target_line_end=5,
            bundle_signature="sig",
            model="claude-test",
            prompt_version="v3",
            purpose="test purpose",
            why="test why",
            constraints=["c1"],
            tradeoffs=[],
            risks=["r1"],
            confidence=0.7,
        )
        authors_module.build_authors(db)
    return tmp_path, sha, db_path, pkg


def test_assemble_returns_full_payload(fixture_repo, fake_codegraph_db) -> None:
    repo_root, sha, wg_db, _ = fixture_repo
    # depth=4 → details computed for every node (the fake codegraph nodes
    # are kind=function so they sit at level 3 by default).
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=fake_codegraph_db,
        whygraph_db=wg_db,
        runtime="static",
        depth=4,
    )
    # Shape — top-level keys.
    assert set(payload.keys()) == {
        "meta",
        "nodes",
        "edges",
        "node_details",
        "dashboard",
        "authors",
    }
    # Meta.
    meta = payload["meta"]
    assert meta["runtime"] == "static"
    assert meta["node_count"] == 3
    assert meta["edge_count"] == 2
    assert meta["rationale_coverage"]["covered"] == 1
    assert meta["rationale_coverage"]["total"] == 3
    # Nodes carry the expected derived fields.
    qns = {n["qualified_name"]: n for n in payload["nodes"]}
    assert qns["pkg.a"]["has_rationale"] is True
    assert qns["pkg.b"]["has_rationale"] is False
    assert qns["pkg.a"]["primary_author"] == "Alice"
    assert qns["pkg.a"]["degree"] == 1  # pkg.a → pkg.b
    # Edges round-trip.
    assert {(e["source"], e["target"]) for e in payload["edges"]} == {
        ("n_a", "n_b"),
        ("n_b", "n_c"),
    }
    # Per-node detail.
    detail_a = payload["node_details"]["n_a"]
    assert detail_a["rationale"]["purpose"] == "test purpose"
    assert detail_a["rationale"]["confidence"] == 0.7
    assert detail_a["contributors"][0]["name"] == "Alice"
    # Activity timeline is bucketed from git blame's committer-time (real
    # wall clock when the test commit ran), so just check the bucket exists.
    assert detail_a["activity"]
    assert sum(detail_a["activity"].values()) >= 1
    assert detail_a["evidence"][0]["sha"] == sha
    assert detail_a["evidence"][0]["prs"][0]["number"] == 42
    # Node without rationale → rationale is None but contributors/evidence present.
    detail_b = payload["node_details"]["n_b"]
    assert detail_b["rationale"] is None
    assert detail_b["contributors"][0]["name"] == "Alice"


def test_assemble_runtime_flag_propagates(fixture_repo, fake_codegraph_db) -> None:
    repo_root, _sha, wg_db, _ = fixture_repo
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=fake_codegraph_db,
        whygraph_db=wg_db,
        runtime="serve",
        depth=4,
    )
    assert payload["meta"]["runtime"] == "serve"


def test_assemble_handles_node_with_missing_file(
    fixture_repo, codegraph_db_factory
) -> None:
    """Node whose file_path doesn't exist on disk should still appear with
    empty contributors/activity/evidence — never crash the render."""
    repo_root, _sha, wg_db, _ = fixture_repo
    cg = codegraph_db_factory(
        nodes=[
            {
                "id": "n_ghost",
                "kind": "function",
                "name": "ghost",
                "qualified_name": "pkg.ghost",
                "file_path": "src/pkg/ghost.py",  # never created
                "language": "python",
                "start_line": 1,
                "end_line": 5,
                "docstring": None,
                "signature": "def ghost()",
            }
        ],
        edges=[],
    )
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=cg,
        whygraph_db=wg_db,
        runtime="static",
        depth=4,
    )
    detail = payload["node_details"]["n_ghost"]
    assert detail["contributors"] == []
    assert detail["activity"] == {}
    assert detail["evidence"] == []
    assert detail["rationale"] is None


def test_assemble_dashboard_and_authors_populated(
    fixture_repo, fake_codegraph_db
) -> None:
    repo_root, _sha, wg_db, _ = fixture_repo
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=fake_codegraph_db,
        whygraph_db=wg_db,
        runtime="static",
        depth=4,
    )
    dash = payload["dashboard"]
    assert dash["repo_overview"]["commits"] == 1
    assert dash["repo_overview"]["pull_requests"] == 1
    assert dash["activity_overall"]  # at least one bucket
    # Authors list contains alice (resolved through the authors table).
    assert payload["authors"]
    alice = payload["authors"][0]
    assert alice["primary_login"] == "alice"
    assert alice["commit_count"] == 1
    assert alice["pr_count"] == 1


def test_assemble_emits_level_per_kind(
    fixture_repo, codegraph_db_factory
) -> None:
    """Each node carries a `level` derived from `kind` per the
    Modules/Classes/Methods/Leaves hierarchy."""
    repo_root, _sha, wg_db, _ = fixture_repo
    cg = codegraph_db_factory(
        nodes=[
            _node("n_file", "file", "f", "f", "src/x.py"),
            _node("n_module", "module", "m", "m", "src/x.py"),
            _node("n_class", "class", "C", "pkg.C", "src/x.py"),
            _node("n_struct", "struct", "S", "pkg.S", "src/x.py"),
            _node("n_func", "function", "fn", "pkg.fn", "src/x.py"),
            _node("n_method", "method", "mth", "pkg.C.mth", "src/x.py"),
            _node("n_var", "variable", "v", "pkg.v", "src/x.py"),
            _node("n_unknown", "weirdkind", "w", "pkg.w", "src/x.py"),
        ],
        edges=[],
    )
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=cg,
        whygraph_db=wg_db,
        runtime="static",
    )
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["n_file"]["level"] == 1
    assert by_id["n_module"]["level"] == 1
    assert by_id["n_class"]["level"] == 2
    assert by_id["n_struct"]["level"] == 2
    assert by_id["n_func"]["level"] == 3
    assert by_id["n_method"]["level"] == 3
    assert by_id["n_var"]["level"] == 4
    # Unmapped kinds default to level 4 so they're not lost.
    assert by_id["n_unknown"]["level"] == 4


def test_assemble_parent_id_uses_contains_edges(
    fixture_repo, codegraph_db_factory
) -> None:
    """When CodeGraph has explicit `contains` edges, they win for parent."""
    repo_root, _sha, wg_db, _ = fixture_repo
    cg = codegraph_db_factory(
        nodes=[
            _node("n_file", "file", "x.py", "src.x", "src/x.py"),
            _node("n_class", "class", "C", "pkg.C", "src/x.py"),
            _node("n_method", "method", "m", "pkg.C.m", "src/x.py"),
        ],
        edges=[
            ("n_file", "n_class", "contains"),
            ("n_class", "n_method", "contains"),
        ],
    )
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=cg,
        whygraph_db=wg_db,
        runtime="static",
    )
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["n_method"]["parent_id"] == "n_class"
    assert by_id["n_class"]["parent_id"] == "n_file"
    assert by_id["n_file"]["parent_id"] is None


def test_assemble_parent_id_falls_back_to_qname_prefix(
    fixture_repo, codegraph_db_factory
) -> None:
    """Without `contains` edges, parent_id is the longest qname prefix
    that matches another node."""
    repo_root, _sha, wg_db, _ = fixture_repo
    cg = codegraph_db_factory(
        nodes=[
            _node("n_pkg", "module", "pkg", "pkg", "src/pkg/__init__.py"),
            _node("n_class", "class", "C", "pkg.C", "src/pkg/x.py"),
            _node("n_method", "method", "m", "pkg.C.m", "src/pkg/x.py"),
        ],
        edges=[],  # no contains edges → fallback path exercised
    )
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=cg,
        whygraph_db=wg_db,
        runtime="static",
    )
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["n_method"]["parent_id"] == "n_class"
    assert by_id["n_class"]["parent_id"] == "n_pkg"


def test_assemble_parent_id_falls_back_to_file_node(
    fixture_repo, codegraph_db_factory
) -> None:
    """If neither contains nor qname-prefix resolves, fall back to the
    file-level node sharing the same `file_path`."""
    repo_root, _sha, wg_db, _ = fixture_repo
    cg = codegraph_db_factory(
        nodes=[
            _node("n_file", "file", "x.py", "x", "src/x.py"),
            # qname doesn't share a prefix with any other node.
            _node("n_func", "function", "orphan", "totally_other", "src/x.py"),
        ],
        edges=[],
    )
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=cg,
        whygraph_db=wg_db,
        runtime="static",
    )
    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id["n_func"]["parent_id"] == "n_file"


def _node(
    id_: str,
    kind: str,
    name: str,
    qname: str,
    file_path: str,
    *,
    start: int = 1,
    end: int = 5,
) -> dict:
    return {
        "id": id_,
        "kind": kind,
        "name": name,
        "qualified_name": qname,
        "file_path": file_path,
        "language": "python",
        "start_line": start,
        "end_line": end,
        "docstring": None,
        "signature": None,
    }


def test_assemble_meta_carries_depth(
    fixture_repo, fake_codegraph_db
) -> None:
    repo_root, _sha, wg_db, _ = fixture_repo
    for d in (1, 2, 3, 4):
        payload = data_module.assemble(
            repo_root=repo_root,
            codegraph_db=fake_codegraph_db,
            whygraph_db=wg_db,
            runtime="static",
            depth=d,
        )
        assert payload["meta"]["depth"] == d


def test_assemble_depth1_only_populates_module_details(
    fixture_repo, codegraph_db_factory
) -> None:
    """At depth=1 only level-1 nodes (file/module/namespace/package) get
    a populated `node_details` entry. Higher-level nodes still appear in
    `nodes[]` but their detail key is absent (the page renders a
    placeholder when clicked)."""
    repo_root, _sha, wg_db, _ = fixture_repo
    cg = codegraph_db_factory(
        nodes=[
            _node("n_file", "file", "x.py", "src.x", "src/x.py"),
            _node("n_module", "module", "pkg", "pkg", "src/x.py"),
            _node("n_class", "class", "C", "pkg.C", "src/x.py"),
            _node("n_func", "function", "fn", "pkg.fn", "src/x.py"),
            _node("n_var", "variable", "v", "pkg.v", "src/x.py"),
        ],
        edges=[],
    )
    payload = data_module.assemble(
        repo_root=repo_root,
        codegraph_db=cg,
        whygraph_db=wg_db,
        runtime="static",
        depth=1,
    )
    details = payload["node_details"]
    # Level 1: present (modules + files).
    assert "n_file" in details
    assert "n_module" in details
    # Levels 2/3/4: absent.
    assert "n_class" not in details
    assert "n_func" not in details
    assert "n_var" not in details
    # Nodes themselves are still all listed.
    ids = {n["id"] for n in payload["nodes"]}
    assert ids == {"n_file", "n_module", "n_class", "n_func", "n_var"}


def test_assemble_rejects_out_of_range_depth(
    fixture_repo, fake_codegraph_db
) -> None:
    repo_root, _sha, wg_db, _ = fixture_repo
    for bad in (0, 5, -1):
        with pytest.raises(ValueError, match="depth"):
            data_module.assemble(
                repo_root=repo_root,
                codegraph_db=fake_codegraph_db,
                whygraph_db=wg_db,
                runtime="static",
                depth=bad,
            )
