"""Tests for the scan's "Core commits" phase - :mod:`whygraph.scan.core_crawler`.

Plan 49 section 9 items 13 (the incremental pass), 13a (choosing the core
files from the CodeGraph index, every fallback and the skip) and 13c (the
``core_bulk_pair`` contents). Every test runs the real crawler against a
throwaway git repository whose history a real :class:`GitCrawler` recorded.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from rich.progress import Progress
from sqlmodel import select

from conftest import build_fake_codegraph_db
from whygraph.db import get_session
from whygraph.db.models import (
    Commit,
    CommitFileChange,
    CoreBulkPair,
    CoreCommit,
    CoreFile,
    CoreRename,
    CoreState,
)
from whygraph.scan import core_crawler
from whygraph.scan.core_crawler import CoreCrawler, select_core_files
from whygraph.scan.git_crawler import GitCrawler
from whygraph.services.codegraph import CodeGraph
from whygraph.services.git import Repository

_LARGE = 3
"""The fixture's bulk threshold: a commit touching more files than this is bulk."""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _commit(root: Path, message: str, files: dict[str, str | bytes]) -> str:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
        _git(root, "add", "--", rel)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _lines(prefix: str, count: int) -> str:
    return "".join(f"{prefix}{i} = {i}\n" for i in range(1, count + 1))


@pytest.fixture
def repo(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """An empty repository on ``main`` as ``cwd``, with a migrated WhyGraph DB."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "tester@example.com")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")
    monkeypatch.chdir(root)
    return root


def _scan(root: Path) -> None:
    crawler = GitCrawler(Progress(), repository=Repository(root, default_branch="main"))
    crawler.run()
    assert crawler.error is None


def _core(
    root: Path, *, wall_sec: float | None = None, large: int = _LARGE
) -> CoreCrawler:
    """Record the history, then run one core pass in this thread."""
    _scan(root)
    crawler = CoreCrawler(
        Progress(),
        repository=Repository(root, default_branch="main"),
        large_commit_file_count=large,
        wall_sec=wall_sec,
    )
    crawler.run()
    assert crawler.error is None
    return crawler


def _core_commits() -> set[str]:
    with get_session() as session:
        return set(session.exec(select(CoreCommit.sha)).all())


def _pairs() -> set[tuple[str, str]]:
    with get_session() as session:
        return {
            (sha, path)
            for sha, path in session.exec(select(CoreBulkPair.sha, CoreBulkPair.path))
        }


def _file_rows() -> dict[str, CoreFile]:
    with get_session() as session:
        return {
            row.path: CoreFile.model_validate(row.model_dump())
            for row in session.exec(select(CoreFile)).all()
        }


def _renames() -> list[tuple[str, str, str]]:
    with get_session() as session:
        return [
            (row.rename_sha, row.pred_path, row.status)
            for row in session.exec(select(CoreRename)).all()
        ]


def _state() -> CoreState | None:
    with get_session() as session:
        state = session.get(CoreState, 1)
        return None if state is None else CoreState.model_validate(state.model_dump())


def _index(
    root: Path,
    refs: dict[str, int],
    *,
    extra_edges: list[tuple[str, str, str]] = (),  # type: ignore[assignment]
    extra_nodes: list[dict] = (),  # type: ignore[assignment]
) -> Path:
    """A CodeGraph index where ``refs[path]`` cross-file ``calls`` edges reach ``path``."""
    db = root / ".codegraph" / "codegraph.db"
    db.parent.mkdir(exist_ok=True)
    if db.exists():
        db.unlink()

    def node(node_id: str, path: str) -> dict:
        return {
            "id": node_id,
            "kind": "function",
            "name": node_id,
            "qualified_name": node_id,
            "file_path": path,
            "language": "python",
            "start_line": 1,
            "end_line": 1,
        }

    nodes = [node("caller", "elsewhere/caller.py")]
    edges: list[tuple[str, str, str]] = []
    for i, (path, n) in enumerate(sorted(refs.items())):
        nodes.append(node(f"t{i}", path))
        edges += [("caller", f"t{i}", "calls")] * n
    build_fake_codegraph_db(
        db, nodes=[*nodes, *extra_nodes], edges=[*edges, *extra_edges]
    )
    return db


# --------------------------------------------------------------------------- #
# The incremental pass (section 9 item 13)
# --------------------------------------------------------------------------- #


def test_a_complete_pass_writes_the_set_and_a_second_pass_reblames_nothing(
    repo: Path,
) -> None:
    first = _commit(repo, "feat: a and b", {"a.py": _lines("a", 5), "b.py": "b\n"})
    second = _commit(repo, "fix: a", {"a.py": _lines("a", 5) + "a6 = 6\n"})

    crawler = _core(repo)

    assert crawler.completed and crawler.warning is None
    assert crawler.reblamed == 2
    assert _core_commits() == {first, second}
    state = _state()
    assert state is not None
    assert state.selection == "all_text" and state.fallback_reason == "no_index"
    assert state.files_selected == 2 and state.files_done == state.files_total == 2
    assert state.completed_at and state.completed_head == second
    assert "core commits" in (crawler.summary or "")
    assert "no usable code index, every text file used" in (crawler.summary or "")

    again = _core(repo)
    assert again.completed and again.reblamed == 0
    assert _core_commits() == {first, second}


def test_a_changed_blob_reblames_one_file_and_a_deleted_path_loses_its_row(
    repo: Path,
) -> None:
    _commit(repo, "init", {"a.py": "a\n", "b.py": "b\n", "c.py": "c\n"})
    _core(repo)

    edit = _commit(repo, "edit a", {"a.py": "a\nmore\n"})
    _git(repo, "rm", "-q", "c.py")
    _git(repo, "commit", "-q", "-m", "drop c")
    crawler = _core(repo)

    assert crawler.reblamed == 1
    rows = _file_rows()
    assert set(rows) == {"a.py", "b.py"}
    assert edit in rows["a.py"].shas.split()
    head = _git(repo, "rev-parse", "HEAD").strip()
    # The unaffected row was checked and moved to the new HEAD.
    assert rows["b.py"].computed_head == head


def test_an_ignore_revs_edit_reblames_everything(repo: Path) -> None:
    init = _commit(repo, "init", {"a.py": "a\n", "b.py": "b\n"})
    _core(repo)

    _commit(repo, "ignore", {".git-blame-ignore-revs": f"{init}\n"})
    crawler = _core(repo)

    assert crawler.reblamed == 3  # a.py, b.py and the new file itself


@pytest.mark.parametrize("change", ["revert", "squash", "rebase", "switch"])
def test_history_changes_with_identical_blobs_reblame_the_touched_paths(
    repo: Path, change: str
) -> None:
    """Acceptance 10: the blob alone would miss each of these."""
    _commit(repo, "init", {"f.py": "one\ntwo\n", "g.py": "g\n"})
    if change == "revert":
        _core(repo)
        edit = _commit(repo, "edit", {"f.py": "one\nTWO\n"})
        _git(repo, "revert", "--no-edit", edit)
        crawler = _core(repo)
        assert crawler.reblamed == 1
        assert set(_file_rows()["f.py"].shas.split()) != set()
        return
    if change == "squash":
        # Blamed on the topic branch, then the same blob lands on main
        # through a squash commit.
        _git(repo, "checkout", "-q", "-b", "topic")
        wip = _commit(repo, "wip", {"f.py": "one\nTWO\n"})
        _core(repo)
        blob = _file_rows()["f.py"].blob_sha
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "-q", "--squash", "topic")
        _git(repo, "commit", "-q", "-m", "squash topic")
        squash = _git(repo, "rev-parse", "HEAD").strip()
        crawler = _core(repo)
        assert crawler.reblamed == 1
        row = _file_rows()["f.py"]
        assert row.blob_sha == blob
        assert squash in row.shas.split() and wip not in row.shas.split()
        return
    if change == "rebase":
        _git(repo, "checkout", "-q", "-b", "topic")
        _commit(repo, "topic edit", {"f.py": "one\nTWO\n"})
        _git(repo, "checkout", "-q", "main")
        _commit(repo, "main edit", {"g.py": "g\nh\n"})
        _git(repo, "checkout", "-q", "topic")
        _core(repo)
        old = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "rebase", "-q", "main")
        # f.py's blob is identical after the rebase; g.py's changed.
        crawler = _core(repo)
        assert crawler.reblamed == 2
        assert old not in _file_rows()["f.py"].shas.split()
        return
    # A branch switch: the same blob of f.py, reached by different commits.
    _git(repo, "checkout", "-q", "-b", "one")
    on_one = _commit(repo, "edit on one", {"f.py": "one\nTWO\n"})
    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "two")
    on_two = _commit(repo, "the same edit on two", {"f.py": "one\nTWO\n"})
    _git(repo, "checkout", "-q", "one")
    _core(repo)
    assert on_one in _file_rows()["f.py"].shas.split()
    _git(repo, "checkout", "-q", "two")
    crawler = _core(repo)
    assert crawler.reblamed == 1
    shas = _file_rows()["f.py"].shas.split()
    assert on_two in shas and on_one not in shas


def test_a_non_ascii_path_is_found_through_the_history_listing(repo: Path) -> None:
    _commit(repo, "init", {"café.py": "a\n", "plain.py": "p\n"})
    _core(repo)
    # A revert pair leaves the blob as it was: only the -z history names it.
    edit = _commit(repo, "edit", {"café.py": "b\n"})
    _git(repo, "revert", "--no-edit", edit)

    crawler = _core(repo)

    assert crawler.reblamed == 1
    assert "café.py" in _file_rows()


def test_binary_and_overlong_files_are_skipped(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_crawler, "CORE_MAX_LINES", 3)
    _commit(
        repo,
        "init",
        {"blob.bin": b"\x00\x01binary", "long.py": _lines("x", 4), "ok.py": "ok\n"},
    )

    crawler = _core(repo)

    rows = _file_rows()
    assert rows["blob.bin"].status == "skipped" and rows["blob.bin"].shas == ""
    assert rows["long.py"].status == "skipped"
    assert rows["ok.py"].status == "ok"
    assert crawler.skipped == 2 and crawler.completed


def test_oversized_blobs_are_not_listed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_crawler, "CORE_MAX_BYTES", 10)
    _commit(repo, "init", {"big.py": "x" * 50 + "\n", "ok.py": "ok\n"})

    _core(repo)

    assert set(_file_rows()) == {"ok.py"}
    assert _state().files_selected == 1  # type: ignore[union-attr]


def _rename_fixture(root: Path) -> dict[str, str]:
    sha = {
        "init": _commit(root, "init", {"old.py": _lines("u", 30), "x.py": "x\n"}),
        "tail": _commit(root, "tail", {"old.py": _lines("u", 20) + "u21 = 210\n"}),
    }
    _git(root, "mv", "old.py", "new.py")
    sha["rename"] = _commit(root, "move", {"new.py": _lines("u", 18)})
    return sha


def test_predecessors_are_in_core_commit(repo: Path) -> None:
    sha = _rename_fixture(repo)

    crawler = _core(repo)

    assert crawler.completed
    assert _renames() == [(sha["rename"], "old.py", "ok")]
    # ``tail`` owns no line of new.py at HEAD: only the predecessor reaches it.
    assert sha["tail"] not in _file_rows()["new.py"].shas.split()
    assert sha["tail"] in _core_commits()


def test_a_failed_file_and_a_failed_rename_are_not_retried(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whygraph.mcp import evidence

    _rename_fixture(repo)

    def boom(*args: object, **kwargs: object) -> set[str]:
        raise RuntimeError("boom")

    with monkeypatch.context() as patch:
        patch.setattr(evidence, "reachable_shas_for_file", boom)
        patch.setattr(evidence, "predecessor_shas", boom)
        crawler = _core(repo)
    assert crawler.failed == 3 and crawler.completed
    assert {row.status for row in _file_rows().values()} == {"failed"}
    assert [status for _, _, status in _renames()] == ["failed"]

    again = _core(repo)
    assert again.reblamed == 0 and again.failed == 0


@pytest.mark.parametrize("root_commit", [False, True])
def test_a_missing_predecessor_is_skipped(repo: Path, root_commit: bool) -> None:
    """The predecessor is not at the parent, or the rename has no parent."""
    sha = _commit(repo, "init", {"a.py": "a\n"})
    if not root_commit:
        sha = _commit(repo, "edit", {"a.py": "a\nb\n"})
    _scan(repo)
    with get_session() as session:
        session.add(
            CommitFileChange(
                commit_sha=sha, path="a.py", change_type="R", renamed_from="ghost.py"
            )
        )

    crawler = _core(repo)

    assert [(pred, status) for _, pred, status in _renames()] == [
        ("ghost.py", "skipped")
    ]
    assert crawler.skipped == 1 and crawler.completed


def test_a_walled_pass_keeps_the_previous_set_and_the_next_pass_resumes(
    repo: Path,
) -> None:
    first = _commit(repo, "init", {"a.py": "a\n", "b.py": "b\n"})
    _core(repo)
    before = _state()
    assert before is not None

    edit = _commit(repo, "edit", {"a.py": "a\nb\n"})
    walled = _core(repo, wall_sec=0)

    assert not walled.completed
    assert walled.left == 1
    assert walled.warning == "1 of 2 core files left for the next Quick rescan"
    assert _core_commits() == {first}
    state = _state()
    assert state is not None
    assert (state.completed_at, state.completed_head) == (
        before.completed_at,
        before.completed_head,
    )
    assert (state.selection, state.fallback_reason, state.files_selected) == (
        before.selection,
        before.fallback_reason,
        before.files_selected,
    )
    assert (state.files_done, state.files_total) == (1, 2)

    resumed = _core(repo, wall_sec=900)
    assert resumed.completed and resumed.reblamed == 1
    assert _core_commits() == {first, edit}


def test_the_completeness_gate_counts_rename_events(repo: Path) -> None:
    _rename_fixture(repo)
    _core(repo)
    with get_session() as session:
        for row in session.exec(select(CoreRename)).all():
            session.delete(row)

    walled = _core(repo, wall_sec=0)

    assert not walled.completed and walled.left == 1


def test_a_merge_stays_and_a_non_merge_bulk_commit_does_not(repo: Path) -> None:
    _commit(repo, "init", {"a.py": "one\ntwo\n", "b.py": "b\n"})
    bulk = _commit(
        repo,
        "bulk",
        {"a.py": "one\ntwo\nthree\n", **{f"gen/{i}.txt": f"{i}\n" for i in range(4)}},
    )
    _git(repo, "checkout", "-q", "-b", "topic")
    _commit(repo, "topic", {"b.py": "b\ntopic\n"})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "main side", {"a.py": "ONE\ntwo\nthree\n"})
    # An evil merge: it adds a line of its own, so blame names it.
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "topic")
    (repo / "b.py").write_text("b\ntopic\nmerge\n")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-q", "-m", "merge topic")
    merge = _git(repo, "rev-parse", "HEAD").strip()

    _core(repo)

    core = _core_commits()
    assert merge in core
    assert bulk not in core
    assert (bulk, "a.py") in _pairs()


def test_an_exception_is_a_warning_and_keeps_the_rows(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init = _commit(repo, "init", {"a.py": "a\n"})
    _core(repo)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("listing broke")

    monkeypatch.setattr(Repository, "tree_entries", boom)
    crawler = _core(repo)

    assert crawler.error is None
    assert crawler.warning == "core commits not refreshed - listing broke"
    assert _core_commits() == {init}
    assert set(_file_rows()) == {"a.py"}


# --------------------------------------------------------------------------- #
# Choosing the core files (section 9 item 13a)
# --------------------------------------------------------------------------- #


@pytest.fixture
def four(repo: Path) -> Path:
    _commit(
        repo,
        "init",
        {"a.py": "a\n", "b.py": "b\n", "c.py": "c\n", "d.py": "d\n"},
    )
    return repo


_FOUR = ("a.py", "b.py", "c.py", "d.py")


def test_selection_takes_files_until_95_percent_of_references(four: Path) -> None:
    """Acceptance 16: 90 + 6 = 96 >= ceil(0.95 x 100) = 95."""
    _index(four, {"a.py": 90, "b.py": 6, "c.py": 4})

    picked = select_core_files(_FOUR, Repository(four))

    assert picked == core_crawler.CoreSelection(
        frozenset({"a.py", "b.py"}), "codegraph", None
    )


def test_self_edges_contains_and_decorates_count_for_nothing(four: Path) -> None:
    _index(
        four,
        {"a.py": 10, "b.py": 10},
        extra_nodes=[
            {
                "id": "inner",
                "kind": "function",
                "name": "inner",
                "qualified_name": "inner",
                "file_path": "c.py",
                "language": "python",
                "start_line": 1,
                "end_line": 1,
            },
            {
                "id": "inner2",
                "kind": "function",
                "name": "inner2",
                "qualified_name": "inner2",
                "file_path": "c.py",
                "language": "python",
                "start_line": 2,
                "end_line": 2,
            },
        ],
        extra_edges=[("inner", "inner2", "calls")] * 50
        + [("caller", "inner", "contains")] * 50
        + [("caller", "inner", "decorates")] * 50,
    )

    picked = select_core_files(_FOUR, Repository(four))

    # Equal in-degrees go in path order; c.py has no counted reference.
    assert picked is not None and picked.paths == {"a.py", "b.py"}
    with CodeGraph(four / ".codegraph" / "codegraph.db") as graph:
        assert graph.file_in_degree(core_crawler.CORE_EDGE_KINDS) == {
            "a.py": 10,
            "b.py": 10,
        }


def test_equal_in_degrees_are_taken_in_path_order(four: Path) -> None:
    _index(four, {"d.py": 5, "c.py": 5, "b.py": 5, "a.py": 5})

    picked = select_core_files(_FOUR, Repository(four))

    # ceil(0.95 x 20) = 19: all four are needed.
    assert picked is not None and picked.paths == set(_FOUR)
    _index(four, {"d.py": 50, "c.py": 1, "b.py": 1})
    picked = select_core_files(_FOUR, Repository(four))
    # ceil(0.95 x 52) = 50: d.py alone reaches it.
    assert picked is not None and picked.paths == {"d.py"}


def test_a_path_only_the_index_names_is_never_selected(four: Path) -> None:
    _index(four, {"gone.py": 1000, "../outside.py": 1000, "a.py": 1})

    picked = select_core_files(_FOUR, Repository(four))

    assert picked is not None and picked.paths == {"a.py"}


def test_a_selected_binary_file_gets_a_skipped_row(repo: Path) -> None:
    _commit(repo, "init", {"a.py": "a\n", "pic.png": b"\x89PNG\x00\x00", "c.py": "c"})
    _index(repo, {"pic.png": 10, "a.py": 10})

    crawler = _core(repo)

    assert crawler.selection is not None
    assert crawler.selection.paths == {"a.py", "pic.png"}
    rows = _file_rows()
    assert rows["pic.png"].status == "skipped" and rows["a.py"].status == "ok"
    assert "c.py" not in rows


def test_no_index_file_falls_back_as_no_index(four: Path) -> None:
    picked = select_core_files(_FOUR, Repository(four))

    assert picked == core_crawler.CoreSelection(
        frozenset(_FOUR), "all_text", "no_index"
    )


def _unusable(root: Path) -> None:
    picked = select_core_files(_FOUR, Repository(root))
    assert picked == core_crawler.CoreSelection(
        frozenset(_FOUR), "all_text", "unusable"
    )


def test_a_symlinked_codegraph_dir_falls_back(four: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    build_fake_codegraph_db(elsewhere / "codegraph.db")
    (four / ".codegraph").symlink_to(elsewhere)
    _unusable(four)


def test_a_tracked_codegraph_dir_falls_back(four: Path) -> None:
    _index(four, {"a.py": 10})
    _git(four, "add", "-f", ".codegraph/codegraph.db")
    _unusable(four)


def test_a_view_named_nodes_falls_back(four: Path) -> None:
    db = _index(four, {"a.py": 10})
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE nodes RENAME TO real_nodes")
        conn.execute("CREATE VIEW nodes AS SELECT * FROM real_nodes")
    _unusable(four)


def test_a_file_that_is_not_a_database_falls_back(four: Path) -> None:
    (four / ".codegraph").mkdir()
    (four / ".codegraph" / "codegraph.db").write_bytes(b"not a database at all" * 100)
    _unusable(four)


def test_the_query_deadline_falls_back(
    four: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Enough edges that the query runs past one progress-handler period.
    _index(four, {"a.py": 3000, "b.py": 3000})
    monkeypatch.setattr(core_crawler, "CORE_INDEX_QUERY_SEC", 0)
    with caplog.at_level("INFO", logger="whygraph.scan.core_crawler"):
        _unusable(four)
    assert "the in-degree query ran past 0 s" in caplog.text


def test_no_cross_file_reference_to_a_head_file_falls_back(four: Path) -> None:
    _index(four, {"elsewhere/other.py": 10})
    _unusable(four)


def test_an_index_without_file_path_falls_back(four: Path) -> None:
    db = four / ".codegraph" / "codegraph.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE edges (source TEXT, target TEXT, kind TEXT)")
    _unusable(four)


@pytest.mark.parametrize("code", [5, 6, 14, 261])
def test_a_busy_or_locked_index_skips_the_phase(
    repo: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """261 is SQLITE_BUSY_RECOVERY: an extended code of BUSY."""
    init = _commit(repo, "init", {"a.py": "a\n"})
    _index(repo, {"a.py": 1})
    _core(repo)
    before = _state()

    def locked(self: object, kinds: tuple[str, ...]) -> dict[str, int]:
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = code  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(CodeGraph, "file_in_degree", locked)
    _commit(repo, "edit", {"a.py": "a\nb\n"})
    crawler = _core(repo)

    assert crawler.warning == "code index busy, core not refreshed"
    assert crawler.error is None and not crawler.completed
    assert _core_commits() == {init}
    after = _state()
    assert after is not None and before is not None
    assert after.model_dump() == before.model_dump()


def test_a_skip_on_a_first_pass_writes_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "init", {"a.py": "a\n"})
    _index(repo, {"a.py": 1})

    def locked(self: object, kinds: tuple[str, ...]) -> dict[str, int]:
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = 5  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(CodeGraph, "file_in_degree", locked)
    _core(repo)

    assert _state() is None and not _file_rows()


def test_another_sqlite_error_falls_back(
    four: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _index(four, {"a.py": 10})

    def corrupt(self: object, kinds: tuple[str, ...]) -> dict[str, int]:
        exc = sqlite3.DatabaseError("database disk image is malformed")
        exc.sqlite_errorcode = 11  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(CodeGraph, "file_in_degree", corrupt)
    _unusable(four)


def test_the_selection_is_stored_only_by_a_complete_pass(repo: Path) -> None:
    _commit(repo, "init", {"a.py": "a\n", "b.py": "b\n"})
    _core(repo)
    state = _state()
    assert state is not None and state.selection == "all_text"

    _index(repo, {"a.py": 10})
    _commit(repo, "edit", {"a.py": "a\nc\n"})
    _core(repo, wall_sec=0)
    state = _state()
    assert state is not None
    assert (state.selection, state.fallback_reason, state.files_selected) == (
        "all_text",
        "no_index",
        2,
    )

    _core(repo)
    state = _state()
    assert state is not None
    assert (state.selection, state.fallback_reason, state.files_selected) == (
        "codegraph",
        None,
        1,
    )
    assert state.completed_head == _git(repo, "rev-parse", "HEAD").strip()


def test_a_moving_selection_keeps_rows_and_reblames_nothing_on_return(
    repo: Path,
) -> None:
    """Acceptance 19."""
    only_b = _commit(repo, "init b", {"b.py": "b\n"})
    only_a = _commit(repo, "init a", {"a.py": "a\n"})
    _index(repo, {"a.py": 10, "b.py": 10})
    _core(repo)
    assert _core_commits() == {only_a, only_b}

    _index(repo, {"a.py": 100, "b.py": 1})
    left = _core(repo)
    assert left.selection is not None and left.selection.paths == {"a.py"}
    assert left.reblamed == 0
    assert "b.py" in _file_rows()
    assert _core_commits() == {only_a}

    _index(repo, {"a.py": 10, "b.py": 10})
    back = _core(repo)
    assert back.reblamed == 0
    assert _core_commits() == {only_a, only_b}


# --------------------------------------------------------------------------- #
# Bulk pairs (section 9 item 13c, the core_bulk_pair half)
# --------------------------------------------------------------------------- #


def test_bulk_pairs_are_the_selected_files_a_bulk_commit_touched(repo: Path) -> None:
    """Acceptance 21: of a selected, an unselected and a renamed file, only
    the first is a pair."""
    _commit(
        repo,
        "init",
        {"core.py": _lines("c", 5), "unsel.py": "u\n", "old.py": _lines("o", 5)},
    )
    bulk = _commit(
        repo,
        "bulk",
        {
            "core.py": _lines("c", 5) + "c6 = 6\n",
            "unsel.py": "u\nv\n",
            "old.py": _lines("o", 5) + "o6 = 6\n",
            **{f"gen/{i}.txt": f"{i}\n" for i in range(2)},
        },
    )
    _git(repo, "mv", "old.py", "new.py")
    _git(repo, "commit", "-q", "-m", "rename old to new")
    _index(repo, {"core.py": 10, "new.py": 10})

    crawler = _core(repo)

    assert crawler.selection is not None
    assert crawler.selection.paths == {"core.py", "new.py"}
    # The bulk commit reaches new.py (blame follows the rename), but its
    # per-file row carries old.py, so it is no pair there.
    assert bulk in _file_rows()["new.py"].shas.split()
    assert _pairs() == {(bulk, "core.py")}
    assert bulk not in _core_commits()
    assert "1 bulk file slices" in (crawler.summary or "")


def test_bulk_pairs_are_replaced_only_by_a_complete_pass(repo: Path) -> None:
    _commit(repo, "init", {"a.py": "a\n"})
    bulk = _commit(repo, "bulk", {"a.py": "a\nb\n", **{f"g{i}": "g" for i in range(4)}})
    _index(repo, {"a.py": 1})
    _core(repo)
    assert _pairs() == {(bulk, "a.py")}

    _commit(repo, "rewrite", {"a.py": "x\n"})
    _core(repo, wall_sec=0)
    assert _pairs() == {(bulk, "a.py")}
    with get_session() as session:
        assert session.get(Commit, bulk) is not None


@pytest.mark.parametrize(
    "module", ["whygraph.mcp.evidence", "whygraph.scan.core_crawler"]
)
def test_the_modules_import_in_a_fresh_interpreter(module: str) -> None:
    """Section 9 item 14: the lazy imports keep ``scan`` -> ``mcp`` acyclic."""
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
