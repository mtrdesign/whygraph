"""Evidence from hunks (M2e step 1): blame origins and the at-SHA walk-past.

Three groups:

* the porcelain parser's :class:`BlameOrigin` ranges, against crafted
  blobs and real throwaway repos;
* :func:`evidence_from_hunks` itself (unknown SHAs, the ignore-revs file);
* the **walk-past equivalence suite**: every scenario runs
  ``evidence_from_hunks`` with the new walk (each boring hunk blamed at its
  own SHA) and with the pre-M2e working-tree walk kept in
  ``tests/_walk_oracle.py``, then compares the bundles as sorted
  ``(sha, source)`` sets plus their order (every commit has a distinct
  timestamp, so the order is fully determined by the set).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _walk_oracle import walk_past_boring_oracle

from whygraph.db import get_session
from whygraph.db.models import Commit
from whygraph.mcp import evidence
from whygraph.mcp.evidence import EvidenceResult, evidence_from_hunks
from whygraph.mcp.targets import Target
from whygraph.scan.refactor_score import BORING_THRESHOLD
from whygraph.services.git import BlameHunk, BlameOrigin, GitError, Repository
from whygraph.services.git.commands import GitBlameCmd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    ).stdout


class _Repo:
    """A throwaway repo whose commits get distinct, increasing timestamps."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "tester@example.com")
        _git(root, "config", "user.name", "Test User")
        _git(root, "config", "commit.gpgsign", "false")
        self.sha: dict[str, str] = {}
        self.when: dict[str, str] = {}

    def write(self, files: dict[str, str]) -> None:
        for path, text in files.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text)

    def commit(
        self, name: str, files: dict[str, str], *, remove: tuple[str, ...] = ()
    ) -> str:
        for path in remove:
            _git(self.root, "rm", "-q", "--", path)
        self.write(files)
        if files:
            _git(self.root, "add", "--", *files)
        stamp = f"2026-01-{len(self.sha) + 1:02d}T00:00:00+00:00"
        _git(
            self.root,
            "commit",
            "-q",
            "-m",
            name,
            env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
        )
        sha = _git(self.root, "rev-parse", "HEAD").strip()
        self.sha[name] = sha
        self.when[name] = stamp
        return sha

    def name_of(self, sha: str) -> str:
        return {v: k for k, v in self.sha.items()}.get(sha, sha[:9])


def _lines(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _seed(
    repo: _Repo, *, boring: set[str] = frozenset(), skip: set[str] = frozenset()
) -> None:
    """One ``commit`` row per commit in ``repo`` (``boring`` ones refactor-heavy)."""
    with get_session() as session:
        for name, sha in repo.sha.items():
            if name in skip:
                continue
            row = Commit(
                sha=sha,
                parent_shas="",
                author_name="Test User",
                author_email="tester@example.com",
                authored_at=repo.when[name],
                committed_at=repo.when[name],
                subject=name,
                body="",
                files_changed=1,
                insertions=1,
                deletions=0,
                scanned_at="2026-06-01T00:00:00+00:00",
                llm_description="diff summary",
            )
            if name in boring:
                row.refactor_score = BORING_THRESHOLD + 10
            session.add(row)


def _old_and_new(
    repo: _Repo,
    target: Target,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(name, source)`` per item, new walk first, oracle second."""
    monkeypatch.chdir(repo.root)
    git_repo = Repository(repo.root)
    hunks = git_repo.blame(target.path, target.line_start, target.line_end)
    new = evidence_from_hunks(git_repo, target, hunks).evidence
    with monkeypatch.context() as patch:
        patch.setattr(evidence, "_walk_past_boring", walk_past_boring_oracle)
        old = evidence_from_hunks(git_repo, target, hunks).evidence

    def named(items):
        return [(repo.name_of(it.commit.sha), it.source) for it in items]

    return named(new), named(old)


def _assert_equivalent(new: list, old: list) -> None:
    assert sorted(new) == sorted(old)
    # Distinct timestamps: the order is determined by the set, so this only
    # pins that nothing re-sorts differently.
    assert new == old


# ---------------------------------------------------------------------------
# The porcelain parser's origins
# ---------------------------------------------------------------------------


def _meta(name: str, epoch: int) -> list[str]:
    return [
        f"author {name}",
        f"author-mail <{name.lower()}@example.com>",
        f"author-time {epoch}",
        "author-tz +0000",
        f"committer {name}",
        f"committer-mail <{name.lower()}@example.com>",
        f"committer-time {epoch}",
        "committer-tz +0000",
        f"summary {name}'s commit",
    ]


def test_blame_porcelain_keeps_origin_ranges(tmp_path: Path) -> None:
    """Origins are at-SHA ranges: consecutive original lines merge, a gap
    splits, and ``final_start`` is where each range sits in the blamed file."""
    sha1, sha2 = "a" * 40, "b" * 40
    blob = _lines(
        f"{sha1} 10 1 2",
        *_meta("Alice", 1_700_000_000),
        "filename src/a.py",
        "\tone",
        f"{sha1} 11 2",
        "\ttwo",
        f"{sha2} 3 3 1",
        *_meta("Bob", 1_700_000_100),
        "filename src/a.py",
        "\tthree",
        # sha1 again, original line 12 follows 11: merged into 10-12 even
        # though its final line (4) is not adjacent to the first group's.
        f"{sha1} 12 4 1",
        "\tfour",
        # A gap at the original side: a new range.
        f"{sha1} 20 5 1",
        "\tfive",
    )
    first, second = BlameHunk.from_porcelain(blob)
    assert first.lines_owned == 4
    assert first.origins == (
        BlameOrigin("src/a.py", 10, 12, 1),
        BlameOrigin("src/a.py", 20, 20, 5),
    )
    assert second.origins == (BlameOrigin("src/a.py", 3, 3, 3),)

    # A real repo: an uncommitted line at the top shifts every final line.
    repo = _Repo(tmp_path / "repo")
    repo.commit("A", {"f.py": _lines("a1", "a2", "a3", "a4", "a5")})
    repo.commit("B", {"f.py": _lines("a1", "a2", "B3", "a4", "a5")})
    repo.write({"f.py": _lines("wt", "a1", "a2", "B3", "a4", "a5")})
    hunks = {h.sha: h for h in Repository(repo.root).blame("f.py", 1, 6)}
    assert hunks["0" * 40].origins == (BlameOrigin("f.py", 1, 1, 1),)
    assert hunks[repo.sha["A"]].origins == (
        BlameOrigin("f.py", 1, 2, 2),
        BlameOrigin("f.py", 4, 5, 5),
    )
    assert hunks[repo.sha["B"]].origins == (BlameOrigin("f.py", 3, 3, 4),)
    # Hand-built hunks keep working: origins default to empty.
    assert BlameHunk("c" * 40, 1, None, None, None, None).origins == ()


def test_blame_origins_follow_a_rename(tmp_path: Path) -> None:
    """A renamed file's lines keep the path they had at the owning commit,
    and blaming that path at that SHA reaches the same lines again."""
    repo = _Repo(tmp_path / "repo")
    a = repo.commit("A", {"old.py": _lines("x = 1", "y = 2", "z = 3")})
    _git(repo.root, "mv", "old.py", "new.py")
    repo.commit("rename", {})
    git_repo = Repository(repo.root)

    (hunk,) = git_repo.blame("new.py", 2, 3)
    assert hunk.sha == a
    assert hunk.origins == (BlameOrigin("old.py", 2, 3, 2),)
    (again,) = git_repo.blame("old.py", ranges=[(2, 3)], rev=a)
    assert again.sha == a
    assert again.origins == (BlameOrigin("old.py", 2, 3, 2),)


def test_blame_filename_per_group(tmp_path: Path) -> None:
    """``filename`` follows every group header once a commit's lines come
    from more than one path; a group without one inherits the SHA's last."""
    sha1 = "a" * 40
    blob = _lines(
        f"{sha1} 1 1 1",
        *_meta("Alice", 1_700_000_000),
        "filename x.py",
        "\tx",
        f"{sha1} 1 2 1",
        "filename y.py",
        "\ty",
        f"{sha1} 2 3 1",  # no filename: still y.py
        "\ty2",
    )
    (hunk,) = BlameHunk.from_porcelain(blob)
    assert hunk.origins == (BlameOrigin("x.py", 1, 1, 1), BlameOrigin("y.py", 1, 2, 2))

    # The real thing: commit M builds z.py from blocks of x.py and y.py.
    repo = _Repo(tmp_path / "repo")
    xs = [
        f"def x_func_{i}(alpha, beta, gamma):  # long enough to score {i}"
        for i in range(8)
    ]
    ys = [
        f"def y_func_{i}(delta, epsilon, zeta):  # long enough to move {i}"
        for i in range(8)
    ]
    a = repo.commit("A", {"x.py": _lines(*xs), "y.py": _lines(*ys)})
    m = repo.commit(
        "M",
        {
            "z.py": _lines(*xs[:4], "# glue written in M", *ys[:4]),
            "x.py": _lines(*xs[4:]),
            "y.py": _lines(*ys[4:]),
        },
    )
    hunks = {h.sha: h for h in Repository(repo.root).blame("z.py", 1, 9)}
    assert hunks[a].origins == (
        BlameOrigin("x.py", 1, 4, 1),
        BlameOrigin("y.py", 1, 4, 6),
    )
    assert hunks[m].origins == (BlameOrigin("z.py", 5, 5, 5),)


def test_blame_quoted_filename_is_unquoted() -> None:
    """Git C-quotes an unusual path in ``filename``; origins carry the real one."""
    blob = _lines(
        f"{'a' * 40} 1 1 1",
        *_meta("Alice", 1_700_000_000),
        'filename "dir/\\303\\251 \\"q\\".py"',
        "\tline",
    )
    (hunk,) = BlameHunk.from_porcelain(blob)
    assert hunk.origins[0].path == 'dir/é "q".py'


def test_blame_several_ranges(temp_git_repo: Path) -> None:
    repo = Repository(temp_git_repo)
    hunks = repo.blame("sample.py", ranges=[(1, 1), (3, 3)])
    assert sum(h.lines_owned for h in hunks) == 2
    argv = GitBlameCmd("f.py", ranges=[(1, 2), (5, 9)]).argv()
    assert "-L1,2" in argv and "-L5,9" in argv
    assert argv.index("-L5,9") < argv.index("--")
    with pytest.raises(ValueError):
        repo.blame("sample.py", 1, 1, ranges=[(1, 1)])
    with pytest.raises(ValueError):
        repo.blame("sample.py")
    with pytest.raises(GitError, match="failed to blame sample.py:1-1,40-41"):
        repo.blame("sample.py", ranges=[(1, 1), (40, 41)])


def test_has_commit_and_remote_refs_containing(tmp_path: Path) -> None:
    repo = _Repo(tmp_path / "repo")
    a = repo.commit("A", {"f.py": _lines("a")})
    b = repo.commit("B", {"f.py": _lines("b")})
    _git(repo.root, "update-ref", "refs/remotes/origin/main", a)
    _git(repo.root, "update-ref", "refs/remotes/upstream/main", b)
    git_repo = Repository(repo.root)

    assert git_repo.has_commit(a)
    assert not git_repo.has_commit("1" * 40)
    blob = _git(repo.root, "rev-parse", "HEAD:f.py").strip()
    assert not git_repo.has_commit(blob)
    assert not git_repo.has_commit("--all")
    assert git_repo.remote_refs_containing(a) == ("refs/remotes/origin/main",)
    # B is only on another remote: not pushed to origin.
    assert git_repo.remote_refs_containing(b) == ()
    assert git_repo.remote_refs_containing("1" * 40) == ()
    assert git_repo.remote_refs_containing("--all") == ()


# ---------------------------------------------------------------------------
# evidence_from_hunks
# ---------------------------------------------------------------------------


def test_evidence_from_hunks_reports_only_blame_unknowns(
    tmp_path: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unknown`` lists the given hunks the DB lacks - not uncommitted
    lines, and not a walked SHA the DB lacks (dropped silently, as ever)."""
    repo = _Repo(tmp_path / "repo")
    repo.commit("A", {"f.py": _lines("x = 1", "y = 1")})
    repo.commit("B", {"f.py": _lines("x = 42", "y = 1")})
    repo.commit("C", {"f.py": _lines("x = 42", "y = 2")})
    repo.write({"f.py": _lines("x = 42", "y = 2", "z = 3")})
    # B is boring and scanned; A (walked to) and C (blamed) are not.
    _seed(repo, boring={"B"}, skip={"A", "C"})
    monkeypatch.chdir(repo.root)
    git_repo = Repository(repo.root)
    target = Target("f.py", 1, 3, None)
    hunks = git_repo.blame("f.py", 1, 3)

    result = evidence_from_hunks(git_repo, target, hunks)

    assert isinstance(result, EvidenceResult)
    assert [(it.commit.sha, it.source) for it in result.evidence] == [
        (repo.sha["B"], "blame")
    ]
    assert [h.sha for h in result.unknown] == [repo.sha["C"]]
    # collect_evidence returns exactly the evidence half.
    assert [it.commit.sha for it in evidence.collect_evidence(target)] == [
        repo.sha["B"]
    ]


def test_ignore_revs_file_checked_inside_root(
    tmp_path: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlinked ``.git-blame-ignore-revs`` is ignored; a real one is
    resolved once per ``evidence_from_hunks`` call and passed to every blame."""
    repo = _Repo(tmp_path / "repo")
    a = repo.commit("A", {"f.py": _lines("x = 1")})
    b = repo.commit("B", {"f.py": _lines("x = 42")})
    outside = tmp_path / "outside-ignore-revs"
    outside.write_text(f"{b}\n")
    (repo.root / ".git-blame-ignore-revs").symlink_to(outside)
    git_repo = Repository(repo.root)

    assert git_repo.blame_ignore_revs_file() is None
    assert git_repo.blame("f.py", 1, 1)[0].sha == b

    (repo.root / ".git-blame-ignore-revs").unlink()
    (repo.root / ".git-blame-ignore-revs").write_text(f"{b}\n")
    resolved = git_repo.blame_ignore_revs_file()
    assert resolved == repo.root / ".git-blame-ignore-revs"
    assert git_repo.blame("f.py", 1, 1)[0].sha == a

    # One resolution per call, the same file on every blame.
    _seed(repo, boring={"A"})
    monkeypatch.chdir(repo.root)
    resolutions: list[None] = []
    files: list[object] = []
    real_resolve = Repository.blame_ignore_revs_file
    real_blame = Repository.blame

    def counting_resolve(self: Repository) -> Path | None:
        resolutions.append(None)
        return real_resolve(self)

    def recording_blame(self: Repository, path: str, *args, **kwargs):
        files.append(kwargs.get("ignore_revs_file"))
        return real_blame(self, path, *args, **kwargs)

    monkeypatch.setattr(Repository, "blame_ignore_revs_file", counting_resolve)
    monkeypatch.setattr(Repository, "blame", recording_blame)
    hunk = BlameHunk(a, 1, None, None, None, None, (BlameOrigin("f.py", 1, 1, 1),))
    evidence_from_hunks(git_repo, Target("f.py", 1, 1, None), [hunk])
    assert len(resolutions) == 1
    assert files and all(f == resolved for f in files)


def test_evidence_from_hunks_git_budget(
    tmp_path: Path,
    whygraph_db_initialized: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past ``git_budget`` blame calls, the remaining hunks stay unwalked."""
    repo = _Repo(tmp_path / "repo")
    repo.commit("A", {"f.py": _lines("x = 1")})
    repo.commit("B", {"f.py": _lines("x = 42")})
    _seed(repo, boring={"B"})
    monkeypatch.chdir(repo.root)
    git_repo = Repository(repo.root)
    target = Target("f.py", 1, 1, None)
    hunks = git_repo.blame("f.py", 1, 1)

    walked = evidence_from_hunks(git_repo, target, hunks, git_budget=1).evidence
    unwalked = evidence_from_hunks(git_repo, target, hunks, git_budget=0).evidence

    assert {it.source for it in walked} == {"blame", "blame-walked"}
    assert [(it.commit.sha, it.source) for it in unwalked] == [(repo.sha["B"], "blame")]


# ---------------------------------------------------------------------------
# Walk-past equivalence: the new at-SHA walk against the working-tree oracle
# ---------------------------------------------------------------------------


def test_walk_equiv_single_hop(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _Repo(tmp_path / "repo")
    repo.commit("K", {"f.py": _lines("keep = 0")})
    repo.commit("A", {"f.py": _lines("keep = 0", "x = 1", "y = 1")})
    repo.commit("B", {"f.py": _lines("keep = 0", "x = 42", "y = 42")})
    repo.commit("C", {"f.py": _lines("top = 9", "keep = 0", "x = 42", "y = 42")})
    _seed(repo, boring={"B"})

    new, old = _old_and_new(repo, Target("f.py", 1, 4, None), monkeypatch)

    assert ("A", "blame-walked") in new
    _assert_equivalent(new, old)


def test_walk_equiv_multi_hop_chain(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three boring commits in a row: the walk reaches the author on hop 3."""
    repo = _Repo(tmp_path / "repo")
    repo.commit("A", {"f.py": _lines("value = compute(alpha, beta)")})
    repo.commit("B1", {"f.py": _lines("value = compute(alpha, bet)")})
    repo.commit("B2", {"f.py": _lines("value = compute(alfa, bet)")})
    repo.commit("B3", {"f.py": _lines("valu = compute(alfa, bet)")})
    _seed(repo, boring={"B1", "B2", "B3"})

    new, old = _old_and_new(repo, Target("f.py", 1, 1, None), monkeypatch)

    assert new == [
        ("B3", "blame"),
        ("B2", "blame-walked"),
        ("B1", "blame-walked"),
        ("A", "blame-walked"),
    ]
    _assert_equivalent(new, old)


def test_walk_equiv_move_within_file(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boring commit that edits a block a later commit moved down the file:
    the boring hunk's origins sit at different line numbers than the working
    tree's, and the at-SHA blame has to use the former."""
    repo = _Repo(tmp_path / "repo")
    block = [f"def handler_{i}(request, response):  # body {i}" for i in range(4)]
    filler = [f"CONSTANT_{i} = 'a long enough constant value {i}'" for i in range(6)]
    repo.commit("F", {"f.py": _lines(*filler)})
    repo.commit("A", {"f.py": _lines(*block, *filler)})
    repo.commit(
        "B", {"f.py": _lines(*(b.replace("request", "req") for b in block), *filler)}
    )
    moved = [b.replace("request", "req") for b in block]
    repo.commit("M", {"f.py": _lines(*filler, *moved)})
    repo.commit("C", {"f.py": _lines("import os", *filler, *moved)})
    _seed(repo, boring={"B"})

    new, old = _old_and_new(repo, Target("f.py", 1, 11, None), monkeypatch)

    assert ("A", "blame-walked") in new
    _assert_equivalent(new, old)


def test_walk_equiv_copy_across_files(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boring hunk's lines now live in z.py but sat in x.py at the boring
    commit (moved by a later commit, ``-C``): the walk blames x.py at B."""
    repo = _Repo(tmp_path / "repo")
    block = [
        f"def x_func_{i}(alpha, beta, gamma):  # long enough to score {i}"
        for i in range(4)
    ]
    rest = [
        f"def x_rest_{i}(alpha, beta, gamma):  # stays behind in x.py {i}"
        for i in range(4)
    ]
    repo.commit("A", {"x.py": _lines(*block, *rest)})
    renamed = [b.replace("alpha", "alfa") for b in block]
    repo.commit("B", {"x.py": _lines(*renamed, *rest)})
    repo.commit("M", {"x.py": _lines(*rest), "z.py": _lines("# moved here", *renamed)})
    _seed(repo, boring={"B"})

    monkeypatch.chdir(repo.root)
    initial = {h.sha: h for h in Repository(repo.root).blame("z.py", 1, 5)}
    assert initial[repo.sha["B"]].origins == (BlameOrigin("x.py", 1, 4, 2),)

    new, old = _old_and_new(repo, Target("z.py", 1, 5, None), monkeypatch)

    assert ("A", "blame-walked") in new
    _assert_equivalent(new, old)


def test_walk_equiv_converging_boring_hunks(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two boring hunks whose lines converge on the same ancestors.

    The new walk blames them in separate calls (the plan's documented
    potential difference, §0.1 #10): git's ignore-rev line guessing and
    ``-M`` move scoring work per blame entry. With git 2.49 both fixtures
    agree with the oracle - the working-tree walk does not coalesce
    entries that reach one commit by different routes either, so it sees
    the same entries one at a time:

    * converge-on-two-authors: B1 rewrote six lines (authored by A and A2),
      B2 rewrote three of them again - both walks reach A and A2;
    * a two-line block moved by M whose lines are later edited by two
      separate boring commits: each line alone scores below ``-M``'s
      threshold, so in **both** walks the lines stop at M, although a plain
      blame of the block at M (one entry) follows the move to A.
    """
    repo = _Repo(tmp_path / "repo")
    lines = [f"result_{i} = compute_value(alpha, beta, {i})" for i in range(6)]
    repo.commit("A", {"f.py": _lines(*lines[:3])})
    repo.commit("A2", {"f.py": _lines(*lines)})
    b1 = [line.replace("compute_value", "compute_val") for line in lines]
    repo.commit("B1", {"f.py": _lines(*b1)})
    repo.commit(
        "B2", {"f.py": _lines(*b1[:3], *(x.replace("alpha", "alfa") for x in b1[3:]))}
    )
    _seed(repo, boring={"B1", "B2"})

    new, old = _old_and_new(repo, Target("f.py", 1, 6, None), monkeypatch)

    assert {name for name, source in new if source == "blame-walked"} == {"A", "A2"}
    _assert_equivalent(new, old)

    moved = _Repo(tmp_path / "moved")
    fill = [f"filler_number_{i} = 'some longer filler text {i}'" for i in range(6)]
    x1, x2 = "abcdef=123456", "ghijkl=789012"
    moved.commit("A", {"f.py": _lines(x1, x2, *fill)})
    moved.commit("M", {"f.py": _lines(*fill, x1, x2)})
    moved.commit("B1", {"f.py": _lines(*fill, x1 + "7", x2)})
    moved.commit("B2", {"f.py": _lines(*fill, x1 + "7", x2 + "7")})
    _seed(moved, boring={"B1", "B2"})
    # The block as one entry at M follows the move to A ...
    at_m = Repository(moved.root).blame("f.py", 7, 8, rev=moved.sha["M"])
    assert [h.sha for h in at_m] == [moved.sha["A"]]

    new, old = _old_and_new(moved, Target("f.py", 7, 8, None), monkeypatch)

    # ... but the walks see one line per entry, and both stop at M.
    assert [name for name, source in new if source == "blame-walked"] == ["M"]
    _assert_equivalent(new, old)


def test_walk_equiv_ignore_revs_file(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checked-in skip list applies to the at-SHA blames too."""
    repo = _Repo(tmp_path / "repo")
    repo.commit("A", {"f.py": _lines("x = 1", "y = 1")})
    repo.commit("S", {"f.py": _lines("x = 2", "y = 1")})
    repo.commit("B", {"f.py": _lines("x = 3", "y = 3")})
    repo.write({".git-blame-ignore-revs": f"{repo.sha['S']}\n"})
    _seed(repo, boring={"B"})

    new, old = _old_and_new(repo, Target("f.py", 1, 2, None), monkeypatch)

    # S is skipped by the file, so line 1 walks past B straight to A.
    assert ("A", "blame-walked") in new
    assert "S" not in {name for name, _ in new}
    _assert_equivalent(new, old)


def test_walk_equiv_git_error_mid_walk(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blame git refuses: the new walk skips that hunk and goes on (rule 3).

    This is a deliberate difference from the oracle, which re-blamed the
    whole working tree in one call and so lost every hunk when it failed
    (``break``). The fault is injected for exactly the blame that has to
    walk past B1: the oracle's single working-tree call, and the new walk's
    at-B1 call.
    """
    repo = _Repo(tmp_path / "repo")
    repo.commit("A", {"f.py": _lines("first = 1", "second = 1")})
    repo.commit("B1", {"f.py": _lines("first = 2", "second = 1")})
    repo.commit("A2", {"f.py": _lines("first = 2", "second = 2")})
    repo.commit("B2", {"f.py": _lines("first = 2", "second = 3")})
    _seed(repo, boring={"B1", "B2"})
    b1 = repo.sha["B1"]
    real_blame = Repository.blame

    def failing_blame(self: Repository, path: str, *args, **kwargs):
        ignore = kwargs.get("ignore_revs") or ()
        if b1 in ignore and kwargs.get("rev") in (None, b1):
            raise GitError("injected")
        return real_blame(self, path, *args, **kwargs)

    monkeypatch.setattr(Repository, "blame", failing_blame)
    monkeypatch.chdir(repo.root)
    git_repo = Repository(repo.root)
    target = Target("f.py", 1, 2, None)
    hunks = real_blame(git_repo, "f.py", 1, 2)
    new = evidence_from_hunks(git_repo, target, hunks).evidence
    with monkeypatch.context() as patch:
        patch.setattr(evidence, "_walk_past_boring", walk_past_boring_oracle)
        old = evidence_from_hunks(git_repo, target, hunks).evidence

    def named(items):
        return sorted((repo.name_of(it.commit.sha), it.source) for it in items)

    assert named(old) == [("B1", "blame"), ("B2", "blame")]
    assert named(new) == [("A2", "blame-walked"), ("B1", "blame"), ("B2", "blame")]


def test_walk_equiv_interleaved_uncommitted_lines(
    tmp_path: Path, whygraph_db_initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncommitted lines between a boring hunk's lines: the hunk's lines are
    contiguous at its SHA (one range) though not in the working tree."""
    repo = _Repo(tmp_path / "repo")
    repo.commit("A", {"f.py": _lines("one = 1", "two = 2", "three = 3")})
    repo.commit("B", {"f.py": _lines("one = 10", "two = 20", "three = 30")})
    repo.write(
        {"f.py": _lines("one = 10", "# local", "two = 20", "# local", "three = 30")}
    )
    _seed(repo, boring={"B"})

    monkeypatch.chdir(repo.root)
    initial = {h.sha: h for h in Repository(repo.root).blame("f.py", 1, 5)}
    assert initial[repo.sha["B"]].origins == (BlameOrigin("f.py", 1, 3, 1),)

    new, old = _old_and_new(repo, Target("f.py", 1, 5, None), monkeypatch)

    assert new == [("B", "blame"), ("A", "blame-walked")]
    _assert_equivalent(new, old)
